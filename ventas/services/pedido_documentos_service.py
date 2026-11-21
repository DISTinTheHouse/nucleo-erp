from __future__ import annotations


def _ordenes_compra_del_pedido(pedido):
    """OC que surten el pedido (#1263): las del M2M ``pedidos`` (todos los que
    surte) y las de la FK ``pedido`` (su primer pedido; alguna OC vieja solo tiene
    la FK). Una consulta, sin duplicados (subconsulta, no JOIN al M2M)."""
    from django.db.models import Q

    from compras.models import OrdenCompra

    del_m2m = OrdenCompra.pedidos.through.objects.filter(pedido_id=pedido.pk).values("ordencompra_id")
    return OrdenCompra.objects.filter(Q(pedido_id=pedido.pk) | Q(pk__in=del_m2m)).order_by("id")


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
        # De todos los pedidos que surte, no solo del primero (#1263): consulta
        # propia en lugar del prefetch de la FK.
        "consulta": _ordenes_compra_del_pedido,
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
        # Principal o ligada por ``pedidos_ids`` (#1086): se lee por
        # ``OrdenProduccionPedido`` (``op_pedidos``), que incluye al principal.
        "related_name": "op_pedidos",
        "via": "op",
        "prefetch_attr": "_documentos_op",
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


def _ops_del_pedido_qs():
    from produccion.models import OrdenProduccionPedido

    return OrdenProduccionPedido.objects.select_related("op").order_by("op_id")


def documentos_related_prefetch_args():
    from django.db.models import Prefetch

    args = []
    for c in DOCUMENTOS_CONFIG:
        if c["is_single"] or c.get("consulta"):
            continue
        if c.get("via"):
            # ``to_attr``: no choca con otros prefetch de ``op_pedidos``.
            args.append(Prefetch(c["related_name"], queryset=_ops_del_pedido_qs(), to_attr=c["prefetch_attr"]))
        else:
            args.append(c["related_name"])
    return args


def _docs_relacionados(pedido, cfg, solo_campos=False):
    if cfg.get("consulta"):
        qs = cfg["consulta"](pedido)
        return _solo_campos(qs, cfg) if solo_campos else qs
    if cfg.get("via"):
        filas = getattr(pedido, cfg["prefetch_attr"], None)
        if filas is None:
            filas = _ops_del_pedido_qs().filter(pedido=pedido)
            if solo_campos:
                filas = _solo_campos(filas, cfg)
        return [getattr(f, cfg["via"]) for f in filas]
    manager = getattr(pedido, cfg["related_name"])
    return _solo_campos(manager.all(), cfg, manager.field.name) if solo_campos else manager.all()


# Documentos cuyo detalle se acota por sucursal: listarlos fuera del alcance
# daba folio/estatus de algo que al abrirlo responde 404 (#536).
TIPOS_POR_SUCURSAL = {"orden_bordado", "orden_reflejante", "orden_corte_manga", "picking", "packing"}


def sucursales_de_documentos(user):
    """Sucursales cuyos documentos ve ``user``; ``None`` = todas las de la
    empresa (superusuario y admin de empresa)."""
    if user.is_superuser or user.is_admin_empresa:
        return None
    return user.sucursales_permitidas()


def alcance_documentos(user):
    """``(ve_facturas, sucursales)``; ``sucursales`` es ``None`` si ve todas.
    Sin ``user`` (``pedido.documentos`` sin petición), todo."""
    from ventas.services.pedido_field_filter_service import puede_ver_contabilidad

    if user is None:
        return True, None
    return puede_ver_contabilidad(user), sucursales_de_documentos(user)


def _de_la_empresa_del_pedido(doc, cfg, pedido):
    """El documento es de la empresa del pedido (EC-916): un dato legado de otra
    empresa que apunta al pedido no se lista. Facturas, con la regla de
    ``pedido_de_la_factura`` (#1203); el resto, por su ``empresa_id`` (entregas y
    devoluciones no lo tienen: solo cuelgan del pedido). En memoria: la lista se
    arma con lo que ``retrieve()`` ya precargó, sin consultas extra."""
    if cfg["tipo"] == "factura":
        from finanzas.services.facturama.cfdi_payload import pedido_de_la_factura

        return pedido_de_la_factura(doc) is not None
    empresa_id = getattr(doc, "empresa_id", None)
    return empresa_id is None or empresa_id == pedido.empresa_id


def _visible(doc, cfg, alcance, pedido):
    if getattr(doc, "activo", True) is False:
        return False
    if not _de_la_empresa_del_pedido(doc, cfg, pedido):
        return False
    ve_facturas, sucursales = alcance
    if cfg["tipo"] == "factura" and not ve_facturas:
        return False
    if cfg["tipo"] in TIPOS_POR_SUCURSAL and sucursales is not None:
        return getattr(doc, "sucursal_id", None) in sucursales
    return True


def _cfg(tipo):
    return next(c for c in DOCUMENTOS_CONFIG if c["tipo"] == tipo)


def campos_de_documento(cfg, modelo):
    """Columnas que leen ``_visible`` y ``fila_de_documento``: el resto puede
    diferirse (``.only()``) cuando no hay prefetch que aprovechar."""
    existentes = {f.name for f in modelo._meta.concrete_fields}
    leidos = {
        modelo._meta.pk.name, cfg["folio_field"], cfg["estatus_field"], cfg["fecha_field"],
        "activo", "empresa", "sucursal",
    }
    return sorted(c for c in leidos if c in existentes)


def campos_diferidos_de_la_cotizacion():
    """``Pedido.cotizacion__<campo>`` que el bloque de la cotización no lee, para
    ``select_related("cotizacion").defer(...)``: sin cargar sus snapshots JSON."""
    from ventas.models import Pedido

    cfg = _cfg("cotizacion")
    modelo = Pedido._meta.get_field(cfg["related_name"]).related_model
    leidos = set(campos_de_documento(cfg, modelo))
    return [
        f"{cfg['related_name']}__{f.name}" for f in modelo._meta.concrete_fields if f.name not in leidos
    ]


def fila_de_documento(cfg, doc):
    """Fila de un documento en ``documentos/`` (EC-920): valor guardado del
    estatus y su etiqueta. ``Cotizacion`` no tiene folio: null, no el ``str(pk)``
    de ``pedido.documentos``."""
    return {
        "id": doc.pk,
        "folio": getattr(doc, cfg["folio_field"], None),
        "estatus": getattr(doc, cfg["estatus_field"]),
        "estatus_label": _estatus_label(doc, cfg["estatus_field"], cfg["estatus_label_field"]),
        "fecha": getattr(doc, cfg["fecha_field"]),
    }


def _solo_campos(qs, cfg, fk_al_pedido=None):
    """``fk_al_pedido``: la FK del manager inverso. El manager la asigna en cada
    fila leyendo su columna; diferida, sería una consulta por fila."""
    if cfg.get("via"):
        modelo = qs.model._meta.get_field(cfg["via"]).related_model
        campos = [f"{cfg['via']}__{c}" for c in campos_de_documento(cfg, modelo)]
        return qs.only(cfg["via"], *campos)
    fk = (fk_al_pedido,) if fk_al_pedido else ()
    return qs.only(*fk, *campos_de_documento(cfg, qs.model))


def documentos_visibles(pedido, user=None, tipos=None, alcance=None, solo_campos=False):
    """``(cfg, doc)`` de cada documento del pedido, en el orden de
    ``DOCUMENTOS_CONFIG``: la relación y las reglas de visibilidad de
    ``listar_documentos_pedido`` (``pedido.documentos``), en un solo lugar para
    que ``documentos/`` (EC-920) no las redefina. Con ``user``: sin bajas, sin
    facturas si no ve contabilidad y, en los tipos por sucursal, solo los de sus
    sucursales; siempre de la empresa del pedido (EC-916). ``tipos``: solo esos.
    Una consulta por tipo, salvo lo que ya venga precargado. ``alcance``: el de
    ``alcance_documentos(user)`` ya calculado. ``solo_campos``: carga solo las
    columnas de ``campos_de_documento`` (sin prefetch de por medio)."""
    if alcance is None:
        alcance = alcance_documentos(user)
    for cfg in DOCUMENTOS_CONFIG:
        if tipos is not None and cfg["tipo"] not in tipos:
            continue
        if cfg["is_single"]:
            doc = getattr(pedido, cfg["related_name"], None)
            if doc is not None and _visible(doc, cfg, alcance, pedido):
                yield cfg, doc
            continue
        try:
            qs = _docs_relacionados(pedido, cfg, solo_campos)
        except Exception:
            continue
        # Iteración directa, NO ``.iterator()``: ``PedidoViewSet.retrieve()``
        # prefetchea las 12 relaciones de ``documentos_related_prefetch_args()``,
        # y ``.iterator()`` nunca lee ``_result_cache`` — re-ejecutaba una
        # consulta por tipo de documento y desperdiciaba todo ese prefetch.
        for doc in qs:
            if _visible(doc, cfg, alcance, pedido):
                yield cfg, doc


def listar_documentos_pedido(pedido, user=None, tipos=None):
    """Documentos del pedido (``documentos_visibles``) con la forma de
    ``pedido.documentos``. ``tipos``: solo esos (un pedido recién creado solo
    puede tener su cotización)."""
    return [
        {
            "id": doc.pk,
            "tipo": cfg["tipo"],
            "label": cfg["label"],
            "folio": _folio(doc, cfg["folio_field"]),
            "fecha": _fecha(doc, cfg["fecha_field"]),
            "estatus": _estatus_label(doc, cfg["estatus_field"], cfg["estatus_label_field"]),
        }
        for cfg, doc in documentos_visibles(pedido, user, tipos)
    ]
