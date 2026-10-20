from __future__ import annotations


DOCUMENTOS_CONFIG = [
    {
        "tipo": "cotizacion",
        "label": "Cotización",
        "related_name": "cotizacion",
        "is_single": True,
        "folio_field": "folio",
        "fecha_field": "created_at",
        "estatus_field": "estatus",
        "estatus_label_field": "get_estatus_display",
    },
    {
        "tipo": "orden_compra",
        "label": "Orden de Compra",
        "related_name": "ordenes_compra",
        "is_single": False,
        "folio_field": "folio",
        "fecha_field": "fecha_oc",
        "estatus_field": "estatus",
        "estatus_label_field": "get_estatus_display",
    },
    {
        "tipo": "factura",
        "label": "Factura",
        "related_name": "facturas",
        "is_single": False,
        "folio_field": "folio",
        "fecha_field": "fecha_emision",
        "estatus_field": "estatus",
        "estatus_label_field": "get_estatus_display",
    },
    {
        "tipo": "orden_produccion",
        "label": "Orden de Producción",
        "related_name": "ordenproduccion_set",
        "is_single": False,
        "folio_field": "folio_op",
        "fecha_field": "fecha_inicio",
        "estatus_field": "estatus_op",
        "estatus_label_field": "get_estatus_op_display",
    },
    {
        "tipo": "orden_bordado",
        "label": "Orden de Bordado",
        "related_name": "ordenesbordado_set",
        "is_single": False,
        "folio_field": "folio_bordado",
        "fecha_field": "fecha_inicio",
        "estatus_field": "estatus_bordado",
        "estatus_label_field": "get_estatus_bordado_display",
    },
    {
        "tipo": "orden_reflejante",
        "label": "Orden de Reflejante",
        "related_name": "ordenesreflejante_set",
        "is_single": False,
        "folio_field": "folio_reflejante",
        "fecha_field": "fecha_inicio",
        "estatus_field": "estatus_reflejante",
        "estatus_label_field": "get_estatus_reflejante_display",
    },
    {
        "tipo": "orden_corte_manga",
        "label": "Orden de Corte de Manga",
        "related_name": "ordenescortemanga_set",
        "is_single": False,
        "folio_field": "folio_ocm",
        "fecha_field": "fecha_inicio",
        "estatus_field": "estatus_corte",
        "estatus_label_field": "get_estatus_corte_display",
    },
    {
        "tipo": "picking",
        "label": "Picking (WMS)",
        "related_name": "pickings",
        "is_single": False,
        "folio_field": "folio",
        "fecha_field": "fecha_inicio",
        "estatus_field": "estado",
        "estatus_label_field": "get_estado_display",
    },
    {
        "tipo": "packing",
        "label": "Packing (WMS)",
        "related_name": "packings",
        "is_single": False,
        "folio_field": "folio",
        "fecha_field": "created_at",
        "estatus_field": "estado",
        "estatus_label_field": "get_estado_display",
    },
    {
        "tipo": "envio",
        "label": "Envío / Guía",
        "related_name": "envios",
        "is_single": False,
        "folio_field": "id",
        "fecha_field": "id",
        "estatus_field": None,
        "estatus_label_field": None,
    },
    {
        "tipo": "entrega",
        "label": "Entrega",
        "related_name": "entregas",
        "is_single": False,
        "folio_field": "id",
        "fecha_field": "id",
        "estatus_field": None,
        "estatus_label_field": None,
    },
    {
        "tipo": "devolucion",
        "label": "Devolución",
        "related_name": "devoluciones",
        "is_single": False,
        "folio_field": "id",
        "fecha_field": "id",
        "estatus_field": None,
        "estatus_label_field": None,
    },
    {
        "tipo": "movimiento_inventario",
        "label": "Movimiento Inventario",
        "related_name": "movimientos_inventario",
        "is_single": False,
        "folio_field": "id",
        "fecha_field": "fecha_movimiento",
        "estatus_field": None,
        "estatus_label_field": None,
    },
]


def _estatus_label(doc, estatus_field, label_method_name):
    if not estatus_field:
        return None
    if label_method_name:
        method = getattr(doc, label_method_name, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    return getattr(doc, estatus_field, None)


def _fecha(doc, fecha_field):
    if not fecha_field:
        return None
    val = getattr(doc, fecha_field, None)
    if val is None:
        return None
    try:
        return val.isoformat()
    except Exception:
        return str(val) if val is not None else None


def _folio(doc, folio_field):
    if not folio_field:
        return str(getattr(doc, "pk", doc.id))
    val = getattr(doc, folio_field, None)
    if val is None or val == "":
        return str(getattr(doc, "pk", doc.id))
    return str(val)


def documentos_related_prefetch_args():
    return [c["related_name"] for c in DOCUMENTOS_CONFIG if not c["is_single"]]


# Documentos cuyo detalle se acota por sucursal: listarlos fuera del alcance
# daba folio/estatus de algo que al abrirlo responde 404 (#536).
TIPOS_POR_SUCURSAL = {"orden_bordado", "orden_reflejante", "orden_corte_manga", "picking", "packing"}


def _alcance(user):
    """``(ve_facturas, sucursales)``; ``sucursales`` es ``None`` si ve todas."""
    from ventas.services.pedido_field_filter_service import puede_ver_contabilidad

    if user is None:
        return True, None
    todas = user.is_superuser or user.is_admin_empresa
    return puede_ver_contabilidad(user), None if todas else user.sucursales_permitidas()


def _visible(doc, cfg, alcance):
    if getattr(doc, "activo", True) is False:
        return False
    ve_facturas, sucursales = alcance
    if cfg["tipo"] == "factura" and not ve_facturas:
        return False
    if cfg["tipo"] in TIPOS_POR_SUCURSAL and sucursales is not None:
        return getattr(doc, "sucursal_id", None) in sucursales
    return True


def listar_documentos_pedido(pedido, user=None):
    """Documentos del pedido. Con ``user``: sin bajas, sin facturas si no ve
    contabilidad y, en los tipos por sucursal, solo los de sus sucursales."""
    documentos = []
    alcance = _alcance(user)
    for cfg in DOCUMENTOS_CONFIG:
        if cfg["is_single"]:
            doc = getattr(pedido, cfg["related_name"], None)
            if doc is None or not _visible(doc, cfg, alcance):
                continue
            documentos.append(
                {
                    "id": doc.pk,
                    "tipo": cfg["tipo"],
                    "label": cfg["label"],
                    "folio": _folio(doc, cfg["folio_field"]),
                    "fecha": _fecha(doc, cfg["fecha_field"]),
                    "estatus": _estatus_label(
                        doc, cfg["estatus_field"], cfg["estatus_label_field"]
                    ),
                }
            )
            continue
        try:
            qs = getattr(pedido, cfg["related_name"]).all()
        except Exception:
            continue
        # Iteración directa, NO ``.iterator()``: ``PedidoViewSet.retrieve()``
        # prefetchea las 12 relaciones de ``documentos_related_prefetch_args()``,
        # y ``.iterator()`` nunca lee ``_result_cache`` — re-ejecutaba una
        # consulta por tipo de documento y desperdiciaba todo ese prefetch.
        for doc in qs:
            if not _visible(doc, cfg, alcance):
                continue
            documentos.append(
                {
                    "id": doc.pk,
                    "tipo": cfg["tipo"],
                    "label": cfg["label"],
                    "folio": _folio(doc, cfg["folio_field"]),
                    "fecha": _fecha(doc, cfg["fecha_field"]),
                    "estatus": _estatus_label(
                        doc, cfg["estatus_field"], cfg["estatus_label_field"]
                    ),
                }
            )
    return documentos
