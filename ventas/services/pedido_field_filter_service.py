import copy
import logging

from seguridad.role_identity import (
    CLAVES_CONTABILIDAD_COMPRAS,
    CLAVES_CONTABILIDAD_VENTAS,
    claves_departamento_usuario,
)

logger = logging.getLogger(__name__)

CAMPOS_CONTABILIDAD_PEDIDO = [
    "anticipo_total", "anticipo_parcial", "pago_antes_embarque",
    "por_confirmar", "otra_cantidad", "monto",
    "envio", "programa_bordados", "bordado_pantalones_extras",
    "serigrafia", "reflejante",
    "flete", "seguros", "anticipo",
    "subtotal", "descuento_global", "ieps", "iva", "gran_total",
    "forma_pago", "metodo_pago", "uso_cfdi",
    "importe_sin_iva", "importe_iva",
]
CAMPOS_CONTABILIDAD_DETALLE = [
    "precio_lista", "precio_unitario", "costo_unitario", "subtotal_linea",
]
CAMPOS_CONTABILIDAD_TALLA = ["precio_unitario", "subtotal_talla"]
CAMPOS_CONTABILIDAD_SERVICIO = ["monto"]

# Los importes de un pedido los ve quien tiene contabilidad en ventas O en compras
# (EC-784): Compras › Pedidos lista los mismos pedidos y necesita sus montos. Hoy
# ``CLAVES_CONTABILIDAD_COMPRAS`` ya incluye las de ventas; la unión explícita deja
# la regla en pie aunque esos dos conjuntos se separen.
CLAVES_IMPORTES_PEDIDO = CLAVES_CONTABILIDAD_VENTAS | CLAVES_CONTABILIDAD_COMPRAS


def puede_ver_contabilidad(user) -> bool:
    """Regla única de importes del pedido: listado, detalle, alta/edición,
    ``editar-mesa-control``, ``kpis``, ``recomprar`` y documentos ligados."""
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "is_admin_empresa", False):
        return True

    claves = claves_departamento_usuario(user)
    return bool(claves & CLAVES_IMPORTES_PEDIDO) if claves else False


def _drop_keys(d: dict, keys: list) -> None:
    if not isinstance(d, dict):
        return
    for k in keys:
        d.pop(k, None)


def quitar_campos_contabilidad_pedido(data) -> None:
    """Quita en sitio los importes de un pedido serializado (y de sus anidados).

    Para listados: el permiso se evalúa una vez por petición y cada fila ya es
    una copia propia del serializer, así que no hace falta ``deepcopy`` (#947).
    """
    _drop_keys(data, CAMPOS_CONTABILIDAD_PEDIDO)

    for det in data.get("detalles") or []:
        _drop_keys(det, CAMPOS_CONTABILIDAD_DETALLE)
        for t in det.get("tallas") or []:
            _drop_keys(t, CAMPOS_CONTABILIDAD_TALLA)

    for s in data.get("servicios_extras") or []:
        _drop_keys(s, CAMPOS_CONTABILIDAD_SERVICIO)


def filtrar_campos_contabilidad_pedido(data, user):
    if puede_ver_contabilidad(user):
        return data

    try:
        cleaned = copy.deepcopy(data)
    except Exception:
        logger.warning("No se pudo copiar serializer.data en filtro contabilidad pedido")
        return data

    quitar_campos_contabilidad_pedido(cleaned)
    return cleaned
