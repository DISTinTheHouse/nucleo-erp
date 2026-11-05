from nucleo.importes import Importes, quitar_importes, sin_importes, tiene_claves_de_importes
from seguridad.role_identity import (
    CLAVES_CONTABILIDAD_COMPRAS,
    CLAVES_CONTABILIDAD_VENTAS,
)

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

# VER los importes de un pedido: contabilidad de ventas O de compras (EC-784):
# Compras › Pedidos lista los mismos pedidos y necesita sus montos. Hoy
# ``CLAVES_CONTABILIDAD_COMPRAS`` ya incluye las de ventas; la unión explícita deja
# la regla en pie aunque esos dos conjuntos se separen.
CLAVES_IMPORTES_PEDIDO = CLAVES_CONTABILIDAD_VENTAS | CLAVES_CONTABILIDAD_COMPRAS
# MODIFICARLOS (#537) y ``recomprar``: sólo contabilidad de ventas. Compras ve los
# montos del pedido pero no los escribe ni clona pedidos en cotizaciones.
CLAVES_EDICION_IMPORTES_PEDIDO = CLAVES_CONTABILIDAD_VENTAS


def puede_ver_contabilidad(user) -> bool:
    """LECTURA de importes del pedido: listado, detalle, respuestas de alta/edición
    y ``editar-mesa-control``, ``kpis``, trazabilidad, documentos ligados, resumen
    del cliente y asistente de IA."""
    return tiene_claves_de_importes(user, CLAVES_IMPORTES_PEDIDO)


def puede_modificar_importes_pedido(user) -> bool:
    """ESCRITURA: campos de contabilidad del pedido (#537) y ``recomprar``, que clona
    sus precios. Es la regla de antes de EC-784, sin la ampliación a compras."""
    return tiene_claves_de_importes(user, CLAVES_EDICION_IMPORTES_PEDIDO)


# Dónde hay importes en un pedido serializado; la usan la copia y la limpieza.
IMPORTES_PEDIDO = Importes(
    tuple(CAMPOS_CONTABILIDAD_PEDIDO),
    {
        "detalles": Importes(tuple(CAMPOS_CONTABILIDAD_DETALLE), {"tallas": Importes(tuple(CAMPOS_CONTABILIDAD_TALLA))}),
        "servicios_extras": Importes(tuple(CAMPOS_CONTABILIDAD_SERVICIO)),
    },
)


def quitar_campos_contabilidad_pedido(data) -> None:
    """Quita en sitio los importes de un pedido serializado (y de sus anidados).

    Para listados: el permiso se evalúa una vez por petición y cada fila ya es
    una copia propia del serializer, así que no hace falta copiar (#947).
    """
    quitar_importes(data, IMPORTES_PEDIDO)


def filtrar_campos_contabilidad_pedido(data, user):
    """``data`` tal cual si el usuario ve importes; si no, una copia sin ellos.
    Falla cerrado: no hay rama que devuelva los importes a quien no los ve."""
    if puede_ver_contabilidad(user):
        return data
    return sin_importes(data, IMPORTES_PEDIDO)
