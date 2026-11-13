import math

from django.db import transaction
from django.db.models import Sum, F
from catalogo.compatibilidad import Proceso, exigir_compatibilidad
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesCorteManga, OrdenCorteMangaDetalle
from produccion.services.common import (
    EPS_CANTIDAD,
    cantidades_asignadas,
    crear_orden_con_guardia_duplicado,
    exigir_producto_en_tallas,
    payload_duplicada,
    pendientes_por_linea,
    revisar_empresa,
    tallas_orden_trabajo_qs,
)
from produccion.utils.folios import generate_ocm_folio
from ventas.models import Pedido, PedidoDetalleTalla


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
        return cantidades_asignadas(OrdenCorteMangaDetalle, "ocm", pedido_ids, "estatus_corte")

    @staticmethod
    def contratado_por_pedido(pedido_ids):
        """Piezas contratadas de corte de manga por pedido, en UNA query.

        Mismo contrato que ``OrdenReflejanteService.contratado_por_pedido``
        (``lleva_corte_manga=True, cantidad > 0``).
        """
        filas = (
            PedidoDetalleTalla.objects
            .filter(
                pedido_detalle__pedido_id__in=list(pedido_ids),
                lleva_corte_manga=True,
                cantidad__gt=0,
            )
            .values("pedido_detalle__pedido_id")
            .annotate(total=Sum("cantidad"))
        )
        return {
            f["pedido_detalle__pedido_id"]: float(f["total"] or 0) for f in filas
        }

    @staticmethod
    def cobertura_por_orden(ordenes, cubierto_override=None, contratado_override=None):
        """Cobertura de cada OCM sobre lo contratado por su pedido.

        Copia de ``OrdenReflejanteService.cobertura_por_orden`` (mismo piso,
        mismo tope ``cubierto <= contratado`` y mismos ``*_override``).
        """
        ordenes = list(ordenes)
        if not ordenes:
            return {}
        if cubierto_override is not None:
            cubierto_por_ocm = cubierto_override
        else:
            cubierto_por_ocm = {
                f["ocm_id"]: float(f["total"] or 0)
                for f in (
                    OrdenCorteMangaDetalle.objects
                    .filter(ocm_id__in=[o.pk for o in ordenes])
                    .values("ocm_id")
                    .annotate(total=Sum("cantidad"))
                )
            }
        if contratado_override is not None:
            contratado = contratado_override
        else:
            contratado = OrdenCorteMangaService.contratado_por_pedido(
                {o.pedido_id for o in ordenes}
            )

        resultado = {}
        for orden in ordenes:
            contratado_int = math.floor(contratado.get(orden.pedido_id, 0.0) + EPS_CANTIDAD)
            cubierto_int = min(
                math.floor(cubierto_por_ocm.get(orden.pk, 0.0) + EPS_CANTIDAD),
                contratado_int,
            )
            resultado[orden.pk] = {
                "cubierto": cubierto_int,
                "contratado": contratado_int,
                "completa": contratado_int > 0 and cubierto_int >= contratado_int,
            }
        return resultado

    @staticmethod
    def partialidad_de_orden(orden, tallas=None):
        """Contexto de parcialidad para el DETALLE de una OCM.

        Devuelve ``(por_linea, por_detalle, hermanas, reparto_aproximado)``;
        mismo contrato que ``OrdenReflejanteService.partialidad_de_orden``.
        3 queries constantes.

        ``tallas``: las tallas que entran a la orden, ya leídas (``leer_mapa_pdt``
        del ``retrieve`` las trae en su misma consulta); ``None`` las consulta aquí.
        """
        if tallas is None:
            tallas = list(
                OrdenCorteMangaService._tallas_corte_manga_qs(orden.pedido_id)
                .order_by("id")
                .values("pedido_detalle_id", "talla_id", "cantidad")
            )
        por_linea_asignado, sin_talla = (
            OrdenCorteMangaService.cantidades_asignadas_por_pedidos([orden.pedido_id])
        )
        calculado = pendientes_por_linea(
            [(t["pedido_detalle_id"], t["talla_id"], float(t["cantidad"] or 0))
             for t in tallas],
            por_linea_asignado,
            sin_talla,
        )
        por_linea = {}
        por_detalle = {}
        for t, (asignada, pendiente) in zip(tallas, calculado):
            contratado = float(t["cantidad"] or 0)
            por_linea[(t["pedido_detalle_id"], t["talla_id"])] = (
                contratado, asignada, pendiente
            )
            acumulado = por_detalle.get(t["pedido_detalle_id"], (0.0, 0.0, 0.0))
            por_detalle[t["pedido_detalle_id"]] = (
                acumulado[0] + contratado,
                acumulado[1] + asignada,
                acumulado[2] + pendiente,
            )

        hermanas = list(
            OrdenesCorteManga.objects
            .filter(pedido_id=orden.pedido_id, activo=True)
            .exclude(pk=orden.pk)
            # Sin canceladas, y con su estatus para distinguir el resto (#1136).
            .exclude(estatus_corte=OrdenesCorteManga.EstatusCorte.CANCELADO)
            .order_by("id")
            .values("id", "folio_ocm", "fecha_inicio", estatus=F("estatus_corte"))
        )
        reparto_aproximado = any(v > EPS_CANTIDAD for v in sin_talla.values())
        return por_linea, por_detalle, hermanas, reparto_aproximado

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

        # Candado de concurrencia por pedido, mismo que OB/OR: sin la constraint
        # ``uq_orden_corte_manga_activa_por_pedido`` (migración ``0025``) dos POST
        # simultáneos leían el mismo asignado y ambos insertaban. En SQLite
        # ``select_for_update`` es no-op; en Postgres es el bloqueo real.
        Pedido.objects.select_for_update().filter(pk=pedido.pk).first()

        detalle_tallas_raw = list(
            OrdenCorteMangaService._tallas_corte_manga_qs(pedido.id).select_related(
                "pedido_detalle", "talla"
            )
        )

        # Matriz prenda/proceso (EC-572): la orden solo revisa su propio proceso (EC-575).
        exigir_compatibilidad(
            pedido.empresa_id,
            OrdenCorteMangaService._tallas_corte_manga_qs(pedido.id),
            procesos={Proceso.CORTE_MANGA},
        )

        if not detalle_tallas_raw:
            raise ValidationError({
                "err": "El pedido no tiene detalles con corte de manga para generar la orden."
            })

        # Cantidad contratada por línea, capturada antes de que el override pise
        # ``dt.cantidad``; evita re-consultar ``PedidoDetalleTalla`` por línea.
        cantidad_pedido_por_id = {
            dt.id: float(dt.cantidad or 0) for dt in detalle_tallas_raw
        }

        detalles_override = data.get("detalles_override") or []
        if detalles_override:
            by_id = {dt.id: dt for dt in detalle_tallas_raw}
            override_by_id = {
                int(item["pedido_detalle_talla_id"]): float(item["cantidad"])
                for item in detalles_override
                if item.get("pedido_detalle_talla_id") is not None
                and item.get("cantidad") is not None
            }

            detalle_tallas = []
            for pdt_id, cantidad in override_by_id.items():
                if pdt_id not in by_id:
                    raise ValidationError({
                        "err": (
                            f"`pedido_detalle_talla_id={pdt_id}` no pertenece "
                            "a este pedido o no lleva servicio de corte de manga."
                        )
                    })
                dt = by_id[pdt_id]
                dt.cantidad = cantidad
                detalle_tallas.append(dt)
        else:
            detalle_tallas = list(detalle_tallas_raw)

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

        asignado_por_linea, asignado_sin_talla = (
            OrdenCorteMangaService._cantidades_asignadas_por_linea(pedido)
        )
        errores_lineas = []
        for dt in detalle_tallas:
            key = (dt.pedido_detalle_id, getattr(dt.talla, "id", None))
            disponible = cantidad_pedido_por_id.get(dt.id, 0.0)
            nuevo = float(dt.cantidad or 0)
            ya = asignado_por_linea.get(key, 0.0)
            faltante = max(0.0, disponible - ya)
            if nuevo > faltante + EPS_CANTIDAD:
                errores_lineas.append(
                    f"  - talla_id={key[1]} pedido_detalle_id={key[0]}: "
                    f"pedido={disponible}, ya_asignado={ya}, solicitado={nuevo}, "
                    f"disponible_restante={faltante}"
                )

        # Segundo corte, por ``pedido_detalle``: absorbe las piezas ya programadas
        # sin talla (``asignado_sin_talla``, p. ej. desde picking), que el corte
        # por línea no ve.
        capacidad_por_detalle = {}
        for dt in detalle_tallas_raw:
            capacidad_por_detalle[dt.pedido_detalle_id] = (
                capacidad_por_detalle.get(dt.pedido_detalle_id, 0.0)
                + cantidad_pedido_por_id.get(dt.id, 0.0)
            )
        solicitado_por_detalle = {}
        for dt in detalle_tallas:
            solicitado_por_detalle[dt.pedido_detalle_id] = (
                solicitado_por_detalle.get(dt.pedido_detalle_id, 0.0)
                + float(dt.cantidad or 0)
            )
        asignado_por_detalle = dict(asignado_sin_talla)
        for (pedido_detalle_id, _talla_id), cantidad in asignado_por_linea.items():
            asignado_por_detalle[pedido_detalle_id] = (
                asignado_por_detalle.get(pedido_detalle_id, 0.0) + cantidad
            )
        for pedido_detalle_id, solicitado in solicitado_por_detalle.items():
            capacidad = capacidad_por_detalle.get(pedido_detalle_id, 0.0)
            ya = asignado_por_detalle.get(pedido_detalle_id, 0.0)
            faltante = max(0.0, capacidad - ya)
            if solicitado > faltante + EPS_CANTIDAD:
                errores_lineas.append(
                    f"  - pedido_detalle_id={pedido_detalle_id} (total del renglón): "
                    f"pedido={capacidad}, ya_asignado={ya}, solicitado={solicitado}, "
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
        exigir_producto_en_tallas(detalle_tallas, "corte de manga")
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
