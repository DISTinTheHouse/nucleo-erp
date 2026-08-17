from __future__ import annotations

import copy
import logging
import re

logger = logging.getLogger(__name__)

CAMPOS_CONTABILIDAD_OC = [
    "subtotal", "descuento", "impuestos", "total",
    "flete", "seguros", "porcentaje_iva", "total_iva",
    "gran_total", "a_cuenta",
]
CAMPOS_CONTABILIDAD_OC_DETALLE = ["precio", "descuento", "importe"]
CAMPOS_CONTABILIDAD_OC_RECEPCION = []
CAMPOS_CONTABILIDAD_OC_RECEPCION_DETALLE = []

TOKENS_ROL_VER_TODO = {"MESACONTROL", "VENTAS", "CONTAVENTAS", "MESACONTROLYVENTAS", "COMPRAS", "CONTABILIDAD", "CONTACOMPRAS"}


def _normalizar_rol_name(s) -> str:
    try:
        return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
    except Exception:
        return ""


def _tokens_roles_usuario(user) -> set:
    tokens = set()
    try:
        asignaciones = user.asignaciones_roles.select_related("rol").all()
    except Exception:
        try:
            asignaciones = user.asignaciones_roles.all()
        except Exception:
            return tokens
    for ur in asignaciones:
        rol = getattr(ur, "rol", None)
        if not rol:
            continue
        for attr in ("codigo", "nombre", "clave_departamento"):
            tok = _normalizar_rol_name(getattr(rol, attr, None))
            if tok:
                tokens.add(tok)
    return tokens


def puede_ver_contabilidad(user) -> bool:
    """Reglas de visibilidad $ en OrdenCompra:

    - `is_superuser` / `is_admin_empresa` → todo.
    - Roles explícitos autorizados a ver números en compras:
      MesaControl, Ventas, ContaVentas, MesaControlYVentas, **Compras**, **Contabilidad**, **ContaCompras**.
    - Cualquier otro rol (incluyendo WMS sin acceso) → se ocultan los keys $$.

    Racional: en compras los roles "Compras"/"Contabilidad"/"ContaCompras" SÍ
    deben ver importes; en el patrón de Pedido solo MesaControl/Ventas. Aquí se
    extiende sin tocar el patrón (lista centralizada).
    """
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "is_admin_empresa", False):
        return True
    tokens = _tokens_roles_usuario(user)
    return bool(tokens & TOKENS_ROL_VER_TODO) if tokens else False


def _drop_keys(d: dict, keys: list) -> None:
    if not isinstance(d, dict):
        return
    for k in keys:
        d.pop(k, None)


def filtrar_campos_contabilidad_orden_compra(data, user):
    if puede_ver_contabilidad(user):
        return data
    try:
        cleaned = copy.deepcopy(data)
    except Exception:
        logger.warning("No se pudo copiar serializer.data en filtro contabilidad OC")
        return data

    _drop_keys(cleaned, CAMPOS_CONTABILIDAD_OC)

    for det in cleaned.get("detalles") or []:
        _drop_keys(det, CAMPOS_CONTABILIDAD_OC_DETALLE)

    for rec in cleaned.get("recepciones") or []:
        _drop_keys(rec, CAMPOS_CONTABILIDAD_OC_RECEPCION)
        for rd in rec.get("detalles") or []:
            _drop_keys(rd, CAMPOS_CONTABILIDAD_OC_RECEPCION_DETALLE)

    return cleaned


DOCUMENTOS_CONFIG = [
    {
        "tipo": "pedido",
        "label": "Pedido",
        "attr": "pedido",
        "single": True,
        "folio_field": "folio",
        "fecha_field": "created_at",
        "estatus_field": "estatus",
        "estatus_label_field": "get_estatus_display",
    },
    {
        "tipo": "solicitud_compra",
        "label": "Solicitud de Compra",
        "attr": "solicitud_compra",
        "single": True,
        "folio_field": None,
        "fecha_field": None,
        "estatus_field": None,
        "estatus_label_field": None,
    },
    {
        "tipo": "recepcion",
        "label": "Recepción",
        "attr": "recepcion_set",
        "single": False,
        "activo_filter": True,
        "folio_field": "folio",
        "fecha_field": "fecha_recepcion",
        "estatus_field": "estatus",
        "estatus_label_field": "get_estatus_display",
    },
    {
        "tipo": "factura_proveedor",
        "label": "Factura Proveedor",
        "attr": "facturas_proveedores",
        "single": False,
        "activo_filter": True,
        "folio_field": "folio",
        "fecha_field": "fecha_emision",
        "estatus_field": "estatus",
        "estatus_label_field": "get_estatus_display",
    },
    {
        "tipo": "movimiento_inventario",
        "label": "Movimiento Inventario",
        "attr": "_recepciones_a_movinv",
        "single": False,
        "folio_field": None,
        "fecha_field": "fecha_movimiento",
        "estatus_field": None,
        "estatus_label_field": None,
    },
]


def _estatus_value(obj, cfg):
    field = cfg.get("estatus_field")
    label_field = cfg.get("estatus_label_field")
    if label_field:
        fn = getattr(obj, label_field, None)
        try:
            if callable(fn):
                value = fn()
            else:
                value = fn
        except Exception:
            value = None
        if value not in (None, ""):
            return value
    if not field:
        return None
    return getattr(obj, field, None)


def _fecha_value(obj, cfg):
    fecha_field = cfg.get("fecha_field")
    if not fecha_field:
        return None
    value = getattr(obj, fecha_field, None)
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _folio_value(obj, cfg, fallback_id):
    folio_field = cfg.get("folio_field")
    if folio_field:
        f = getattr(obj, folio_field, None)
        if f not in (None, ""):
            return str(f)
    return str(fallback_id)


def _iterar_candidatos(orden_compra, cfg):
    attr = cfg["attr"]
    single = cfg.get("single", False)

    if attr == "_recepciones_a_movinv":
        seen_ids = set()
        # ``recepcion_set`` es un RelatedManager: no es iterable sin ``.all()``
        # (iterarlo directo lanza TypeError y tumba el retrieve completo).
        recepcion_manager = getattr(orden_compra, "recepcion_set", None)
        for recepcion in (recepcion_manager.all() if recepcion_manager is not None else []):
            if not getattr(recepcion, "activo", True):
                continue
            qs = getattr(recepcion, "movimientoinventario_set", None)
            if qs is None:
                continue
            try:
                iterator = qs.filter(activo=True).all()
            except Exception:
                try:
                    iterator = qs.all()
                except Exception:
                    iterator = []
            for mov in iterator:
                mov_id = getattr(mov, "pk", None) or getattr(mov, "id", None)
                if mov_id is None or mov_id in seen_ids:
                    continue
                seen_ids.add(mov_id)
                yield mov
        return

    value = getattr(orden_compra, attr, None)

    if single:
        if value is None:
            return
        yield value
        return

    if value is None:
        return

    activo_filter = cfg.get("activo_filter", False)
    try:
        qs = value.filter(activo=True) if activo_filter else value.all()
    except Exception:
        qs = value
    try:
        iterator = qs.iterator()
    except Exception:
        try:
            iterator = iter(qs)
        except Exception:
            iterator = [qs]
    for item in iterator:
        yield item


def listar_documentos_orden_compra(orden_compra):
    """Documentos ligados a ``OrdenCompra`` en formato plano listo para FE.

    Cada entrada: ``{id, tipo, label, folio, fecha, estatus}``.
    Para que el FE abra modal con el detalle real, usa ``tipo + id``.
    """
    items = []
    for cfg in DOCUMENTOS_CONFIG:
        tipo = cfg["tipo"]
        label = cfg["label"]
        for obj in _iterar_candidatos(orden_compra, cfg):
            obj_id = getattr(obj, "pk", None) or getattr(obj, "id", None)
            if obj_id is None:
                continue
            items.append({
                "id": obj_id,
                "tipo": tipo,
                "label": label,
                "folio": _folio_value(obj, cfg, obj_id),
                "fecha": _fecha_value(obj, cfg),
                "estatus": _estatus_value(obj, cfg),
            })
    return items


def documentos_related_prefetch_args():
    """Prefetch paths para que ``listar_documentos_orden_compra`` no haga N+1.

    - ``facturas_proveedores``: directo desde OrdenCompra (related_name).
    - ``recepcion_set``: ya viene prefetch desde el ViewSet; aquí sólo
      aseguramos los movimientos de inventario anidados por recepción.
    - ``pedido`` / ``solicitud_compra``: FK simple → ``select_related``.
    """
    return [
        "facturas_proveedores",
        "recepcion_set",
        "recepcion_set__movimientoinventario_set",
    ]


def armar_pedido_vinculado(orden_compra):
    """Helper minimalista: ``{id, folio}`` del pedido madre o ``None``.

    OrdenCompra.pedido es ``null=True`` (modelo compras.OrdenCompra#L119).
    """
    pedido = getattr(orden_compra, "pedido", None)
    if not pedido:
        return None
    return {"id": pedido.pk, "folio": str(getattr(pedido, "folio", pedido.pk) or pedido.pk)}
