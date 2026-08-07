from django.db import transaction
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesCorteManga, OrdenCorteMangaDetalle
from produccion.services.common import (
    EPS_CANTIDAD,
    cantidades_asignadas,
    crear_orden_con_guardia_duplicado,
    payload_duplicada,
    revisar_empresa,
    tallas_orden_trabajo_qs,
)
from produccion.utils.folios import generate_ocm_folio


class OrdenCorteMangaDuplicada409(APIException):
    status_code = 409
    default_detail = "Ya existe una orden de corte de manga activa para este pedido."
    default_code = "orden_corte_manga_duplicada"


class OrdenCorteMangaService:

    @staticmethod
    def _validar_contexto(pedido, user):
        """Scope empresa/sucursal del pedido contra el usuario que solicita.

        Mismo criterio y mismos mensajes que
        ``OrdenBordadoService._validar_contexto`` / ``OrdenReflejanteService._validar_contexto``.
        """
        resultado = revisar_empresa(user, pedido)
        if resultado == "sin_empresa":
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if resultado == "otra_empresa":
            raise ValidationError("El pedido no pertenece a la empresa del usuario.")

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        if not es_staff and pedido.sucursal_id not in user.sucursales_permitidas():
            raise ValidationError(
                "No tiene acceso a la sucursal del pedido para generar la orden "
                "de corte de manga."
            )

    @staticmethod
    def _tallas_corte_manga_qs(pedido_id):
        return tallas_orden_trabajo_qs(pedido_id, "lleva_corte_manga")

    @staticmethod
    def _payload_duplicada(existente):
        return payload_duplicada(
            existente,
            folio_field="folio_ocm",
            estatus_display="get_estatus_corte_display",
            estatus_field="estatus_corte",
            payload_key="orden_corte_manga_existente",
            tipo_label="corte de manga",
            dividir_label="el corte",
        )

    @staticmethod
    def buscar_existente_full_match(pedido):
        """Devuelve una OCM activa si el pedido ya está cubierto al 100%.

        "Cubierto" se mide en **piezas**, no en número de renglones: hay que
        comparar lo asignado por las OCMs activas contra
        ``PedidoDetalleTalla.cantidad`` línea por línea. Contar renglones (lo
        que hacía antes) trataba una OCM parcial que toca todas las líneas con
        cantidades reducidas como cobertura total, y devolvía un 409 diciendo
        "ya existe ... con el 100% de las prendas" sobre un pedido cubierto a
        medias.

        Sólo se consulta en el POST **sin** ``detalles_override``. Si queda
        cualquier saldo, devuelve ``None`` y el chequeo de cupo de ``save()``
        produce el 400 con las cantidades exactas que faltan.

        Misma regla que ``OrdenBordadoService.buscar_existente_full_match``.
        """
        tallas = list(OrdenCorteMangaService._tallas_corte_manga_qs(pedido.id))
        if not tallas:
            return None

        por_linea, sin_talla = OrdenCorteMangaService._cantidades_asignadas_por_linea(pedido)
        for dt in tallas:
            asignado = por_linea.get((dt.pedido_detalle_id, dt.talla_id), 0.0)
            if asignado + EPS_CANTIDAD < float(dt.cantidad or 0):
                return None
        if any(v > EPS_CANTIDAD for v in sin_talla.values()):
            # Hay piezas programadas sin talla identificable (ver
            # ``cantidades_asignadas_por_pedidos``): no se puede afirmar
            # cobertura exacta, así que no se emite el 409 de duplicado.
            return None

        return (
            OrdenesCorteManga.objects.filter(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                activo=True,
            )
            .order_by("-id")
            .first()
        )

    @staticmethod
    def cantidades_asignadas_por_pedidos(pedido_ids):
        """``common.cantidades_asignadas`` para OCMs; ver allí el contrato."""
        return cantidades_asignadas(OrdenCorteMangaDetalle, "ocm", pedido_ids)

    @staticmethod
    def _cantidades_asignadas_por_linea(pedido):
        """Suma lo ya programado en OCMs activas por cada línea (pedido_detalle, talla).

        Retorna ``(por_linea, sin_talla)``; solo considera
        ``OrdenesCorteManga.activo=True``.
        """
        return OrdenCorteMangaService.cantidades_asignadas_por_pedidos([pedido.pk])

    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.get("pedido")

        OrdenCorteMangaService._validar_contexto(pedido, user)

        sucursal = user.sucursal_default
        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

        detalle_tallas_raw = list(
            OrdenCorteMangaService._tallas_corte_manga_qs(pedido.id).select_related(
                "pedido_detalle", "talla"
            )
        )

        if not detalle_tallas_raw:
             raise ValidationError({
                "err": "El pedido no tiene detalles con corte de manga para generar la orden."
            })

        detalles_override = data.get("detalles_override") or []
        if detalles_override:
            override_map = {
                int(item["pedido_detalle_talla_id"]): float(item["cantidad"])
                for item in detalles_override
            }
            detalle_tallas = [
                next((dt for dt in detalle_tallas_raw if dt.id == pdt_id), None)
                for pdt_id in override_map
            ]
            detalle_tallas = [dt for dt in detalle_tallas if dt is not None]
            for dt in detalle_tallas:
                try:
                    dt.cantidad = override_map[dt.id]
                except AttributeError:
                    pass
        else:
            detalle_tallas = detalle_tallas_raw

        if not detalle_tallas:
            raise ValidationError({
                "err": "No se seleccionaron líneas para generar la orden de corte de manga."
            })

        # El 409 de pedido completo va **antes** del cupo por línea: un POST sin
        # override sobre un pedido ya cubierto al 100% es el duplicado clásico y
        # debe contestar 409, no el 400 de exceso —que es lo que pasaba al
        # evaluar el cupo primero, porque ese caso también agota el cupo—.
        # Sin override se pide el pedido completo tal cual salió de
        # ``_tallas_corte_manga_qs``, así que la condición es exactamente esa.
        # Mismo orden que ``OrdenBordadoService.save``.
        if not detalles_override:
            existente = OrdenCorteMangaService.buscar_existente_full_match(pedido)
            if existente is not None:
                raise OrdenCorteMangaDuplicada409(
                    OrdenCorteMangaService._payload_duplicada(existente)
                )

        asignado_por_linea, _asignado_sin_talla = (
            OrdenCorteMangaService._cantidades_asignadas_por_linea(pedido)
        )
        errores_lineas = []
        for dt in detalle_tallas:
            key = (dt.pedido_detalle_id, getattr(dt.talla, "id", None))
            try:
                from ventas.models import PedidoDetalleTalla
                original = PedidoDetalleTalla.objects.filter(pk=dt.id).values("cantidad").first()
                disponible = float((original or {}).get("cantidad") or 0)
            except Exception:
                disponible = 0.0
            nuevo = float(getattr(dt, "cantidad", None) or 0)
            ya = asignado_por_linea.get(key, 0.0)
            faltante = max(0.0, disponible - ya)
            if nuevo > faltante:
                errores_lineas.append(
                    f"  - talla_id={key[1]} pedido_detalle_id={key[0]}: "
                    f"pedido={disponible}, ya_asignado={ya}, solicitado={nuevo}, "
                    f"disponible_restante={faltante}"
                )
        if errores_lineas:
            raise ValidationError({
                "err": (
                    "No se puede generar la orden de corte de manga: una o más líneas "
                    "exceden la cantidad disponible del pedido."
                ),
                "detalles_exceso": errores_lineas,
            })

        # El folio se consume DESPUÉS de todas las validaciones, para que un
        # rechazo no gaste consecutivo de la serie.
        folio_ocm = generate_ocm_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_corte_manga = crear_orden_con_guardia_duplicado(
            OrdenesCorteManga,
            pedido,
            dict(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_ocm=folio_ocm,
                usuario_asignado=user,
                prioridad=data.get("prioridad", 1),
                observaciones=data.get("observaciones"),
            ),
            OrdenCorteMangaDuplicada409,
            OrdenCorteMangaService._payload_duplicada,
        )

        bulk_data = []
        for dt in detalle_tallas:
            cfg = getattr(dt, "corte_manga_config", None) or {}
            bulk_data.append(OrdenCorteMangaDetalle(
                ocm=orden_corte_manga,
                pedido_detalle=dt.pedido_detalle,
                producto_id=dt.pedido_detalle.producto_id,
                cantidad=float(getattr(dt, "cantidad", None) or 0),
                talla=dt.talla,
                color=getattr(dt.pedido_detalle, "color", None),
                configuracion=cfg if isinstance(cfg, dict) and cfg else None,
            ))

        OrdenCorteMangaDetalle.objects.bulk_create(bulk_data)
        return orden_corte_manga
