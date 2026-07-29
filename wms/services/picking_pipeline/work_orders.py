from produccion.models import (
    OrdenBordadoDetalle,
    OrdenCorteMangaDetalle,
    OrdenesBordado,
    OrdenesCorteManga,
    OrdenesReflejante,
    OrdenReflejanteDetalle,
)
from wms.models import PickingOrdenTrabajo
from wms.utils.folios import generate_folio_multi_tipo


def _bordado_detalle_extra(talla):
    cfg = getattr(talla, "bordado_config", None)
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "posicion_bordado": cfg.get("posicion"),
        "colores_hilo": int(cfg.get("colores_hilo", 0)),
        "puntadas": int(cfg.get("puntadas", 0)),
    }


def _reflejante_detalle_extra(talla):
    cfg = getattr(talla, "reflejante_config", None)
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "tipo_reflejante": cfg.get("tipo_reflejante"),
        "posicion": cfg.get("posicion"),
        "metros": float(cfg.get("metros") or 0.0),
    }


def _corte_manga_detalle_extra(talla):
    cfg = getattr(talla, "corte_manga_config", None)
    cfg = cfg if isinstance(cfg, dict) else {}
    return {"configuracion": cfg or None}


#: Tabla dispatch para los 3 tipos de orden de trabajo (Bordado /
#: Reflejante / Corte de Manga). Cada renglón difiere solo en:
#:
#: - el ``flag`` del request que enciende la OT para la talla,
#: - los ``tipos_documento`` probados al buscar ``SerieFolio``,
#: - el modelo de orden y su detalle,
#: - el nombre de la columna de folio y de la FK del detalle,
#: - el callback que extrae columnas de config desde la talla,
#: - el valor del enum ``PickingOrdenTrabajo.TipoOrden`` y su FK enlace.
#:
#: Todo lo demás (resolver folio, crear orden, ``bulk_create`` de detalles,
#: enlace al picking) se implementa **una sola vez** en ``generar_ordenes``.
ORDENES_TRABAJO_CONFIG = (
    {
        "flag": "generar_orden_bordado",
        "tipos_documento": ["ORDEN_BORDADO", "Orden de Bordado", "Bordado"],
        "descripcion_documento": "las órdenes de bordado",
        "orden_model": OrdenesBordado,
        "folio_field": "folio_bordado",
        "detalle_model": OrdenBordadoDetalle,
        "detalle_fk": "ob",
        "detalle_extra": _bordado_detalle_extra,
        "tipo_orden": PickingOrdenTrabajo.TipoOrden.BORDADO,
        "tipo_resultado": "BORDADO",
        "enlace_field": "orden_bordado",
    },
    {
        "flag": "generar_orden_reflejante",
        "tipos_documento": ["ORDEN_REFLEJANTE", "Orden de Reflejante", "Reflejante"],
        "descripcion_documento": "las órdenes de reflejante",
        "orden_model": OrdenesReflejante,
        "folio_field": "folio_reflejante",
        "detalle_model": OrdenReflejanteDetalle,
        "detalle_fk": "orden_r",
        "detalle_extra": _reflejante_detalle_extra,
        "tipo_orden": PickingOrdenTrabajo.TipoOrden.REFLEJANTE,
        "tipo_resultado": "REFLEJANTE",
        "enlace_field": "orden_reflejante",
    },
    {
        "flag": "generar_orden_corte_manga",
        "tipos_documento": [
            "ORDEN_CORTE_MANGA",
            "Orden Corte de Manga",
            "Orden de Corte de Manga",
            "CorteManga",
        ],
        "descripcion_documento": "las órdenes de corte de manga",
        "orden_model": OrdenesCorteManga,
        "folio_field": "folio_ocm",
        "detalle_model": OrdenCorteMangaDetalle,
        "detalle_fk": "ocm",
        "detalle_extra": _corte_manga_detalle_extra,
        "tipo_orden": PickingOrdenTrabajo.TipoOrden.CORTE_MANGA,
        "tipo_resultado": "CORTE_MANGA",
        "enlace_field": "orden_corte_manga",
    },
)


def generar_ordenes(picking, requested_items, user):
    """Genera las órdenes de trabajo configuradas y las enlaza al picking.

    Para cada tipo de orden con al menos una talla marcada:
      1. Consigue un folio nuevo (``generate_folio_multi_tipo``; lanza 400
         claro si falta la SerieFolio —no hay fallback inventado para no
         romper ``unique`` en caso de picking parcial repetido).
      2. Crea la cabecera de la orden (instancia Usuario, no pk).
      3. ``bulk_create`` de sus detalles (uno por talla del picking).
      4. Construye el enlace ``PickingOrdenTrabajo``.

    Finalmente ``bulk_create`` de todos los enlaces y devuelve la lista
    ``[{tipo, id, folio}, ...]`` que el ``PickingService`` inyecta en la
    respuesta del POST como ``ordenes_trabajo_generadas``.
    """
    resultado = []
    enlaces = []

    for cfg in ORDENES_TRABAJO_CONFIG:
        items = [it for it in requested_items if it[cfg["flag"]]]
        if not items:
            continue

        folio = generate_folio_multi_tipo(
            picking.empresa_id,
            picking.sucursal_id,
            tipos_documento=cfg["tipos_documento"],
            descripcion_documento=cfg["descripcion_documento"],
        )
        orden = cfg["orden_model"].objects.create(
            empresa=picking.empresa,
            sucursal=picking.sucursal,
            pedido=picking.pedido,
            prioridad=1,
            usuario_asignado=user if getattr(user, "pk", None) else None,
            observaciones=f"Generada automáticamente desde picking {picking.folio}.",
            activo=True,
            **{cfg["folio_field"]: folio},
        )

        detalles = []
        for item in items:
            talla = item["talla"]
            variante = getattr(talla, "variante", None)
            detalles.append(
                cfg["detalle_model"](
                    pedido_detalle=talla.pedido_detalle,
                    producto=talla.pedido_detalle.producto,
                    cantidad=float(item["cantidad"]),
                    talla=getattr(variante, "talla", None),
                    color=getattr(variante, "color", None),
                    **{cfg["detalle_fk"]: orden},
                    **cfg["detalle_extra"](talla),
                )
            )
        cfg["detalle_model"].objects.bulk_create(detalles)

        folio_generado = getattr(orden, cfg["folio_field"])
        resultado.append(
            {"tipo": cfg["tipo_resultado"], "id": orden.pk, "folio": folio_generado}
        )
        enlaces.append(
            PickingOrdenTrabajo(
                picking=picking,
                tipo_orden=cfg["tipo_orden"],
                **{cfg["enlace_field"]: orden},
            )
        )

    if enlaces:
        PickingOrdenTrabajo.objects.bulk_create(enlaces)
    return resultado
