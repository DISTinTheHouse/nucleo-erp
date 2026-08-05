from django.db import transaction
from django.db.models import Count, Sum
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesCorteManga, OrdenCorteMangaDetalle
from produccion.services.common import (
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
        """Devuelve OrdenesCorteManga activa si ya cubre 100% de las tallas con lleva_corte_manga.

        Regla SAFE minimalista: misma cantidad de detalle_tallas que el pedido.
        Si negocio decide habilitar fraccionamiento (OCM parcial), esta función
        regresa None y se permite una segunda OCM.
        """
        tallas_esperadas_qty = OrdenCorteMangaService._tallas_corte_manga_qs(
            pedido.id
        ).count()
        if tallas_esperadas_qty == 0:
            return None

        ocm_match = (
            OrdenesCorteManga.objects.filter(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                activo=True,
            )
            .annotate(detalle_count=Count("detalles"))
            .filter(detalle_count=tallas_esperadas_qty)
            .order_by("-id")
            .first()
        )
        return ocm_match

    @staticmethod
    def _cantidades_asignadas_por_linea(pedido):
        """Suma lo ya programado en OCMs activas por cada línea (pedido_detalle, talla).

        Retorna dict ``{(pedido_detalle_id, talla_id): cantidad_asignada}``.
        Solo considera ``OrdenesCorteManga.activo=True``.
        """
        filas = (
            OrdenCorteMangaDetalle.objects
            .filter(ocm__pedido=pedido, ocm__activo=True)
            .values("pedido_detalle_id", "talla_id")
            .annotate(asignado=Sum("cantidad"))
        )
        return {
            (f["pedido_detalle_id"], f["talla_id"]): float(f["asignado"] or 0)
            for f in filas
        }

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

        asignado_por_linea = OrdenCorteMangaService._cantidades_asignadas_por_linea(pedido)
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

        es_full_match = (
            not detalles_override
            and {dt.id for dt in detalle_tallas_raw} == {dt.id for dt in detalle_tallas}
            and all(
                float(dt.cantidad or 0) == float(raw.cantidad or 0)
                for dt, raw in zip(
                    sorted(detalle_tallas, key=lambda x: x.id),
                    sorted(detalle_tallas_raw, key=lambda x: x.id),
                )
            )
        )
        if es_full_match:
            existente = OrdenCorteMangaService.buscar_existente_full_match(pedido)
            if existente is not None:
                raise OrdenCorteMangaDuplicada409(
                    OrdenCorteMangaService._payload_duplicada(existente)
                )

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
