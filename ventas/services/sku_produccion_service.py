"""SKU de producción (``catalogo.VarianteProductoProduccion``) de las tallas de un pedido.

Única definición de "esta talla ya tiene SKU de producción" en ventas: variante
ACTIVA de esa (``pedido_detalle``, ``talla``) y con ``sku`` no vacío. La usan el
campo ``sku_produccion`` del detalle del pedido y ``vincular-sku-muestra``, para
que busquen la variante con el mismo criterio.

El campo y ``vincular-sku-muestra`` exigen además que la línea siga siendo MUESTRA
(``es_muestra``): en una línea convertida a producto de catálogo no hay SKU que vincular.
"""

from django.db.models import Prefetch, Q

from catalogo.models import VarianteProductoProduccion
from ventas.models import PedidoDetalle

# ``to_attr`` del prefetch sobre ``PedidoDetalle``; no pisa el manager inverso
# ``variantes_produccion``.
VARIANTES_PRODUCCION_ATTR = "variantes_produccion_activas"


def variantes_produccion_activas():
    return VarianteProductoProduccion.objects.filter(activo=True)


def variantes_produccion_prefetch():
    """Prefetch (sobre ``PedidoDetalle``) de sus variantes de producción activas."""
    return Prefetch(
        "variantes_produccion",
        queryset=variantes_produccion_activas().only(
            "id", "sku", "pedido_detalle_id", "talla_id"
        ),
        to_attr=VARIANTES_PRODUCCION_ATTR,
    )


def variante_produccion_de_talla(pedido_detalle_id, talla_id):
    """Variante con SKU de esa talla de la línea, o ``None``."""
    variante = (
        variantes_produccion_activas()
        .filter(pedido_detalle_id=pedido_detalle_id, talla_id=talla_id)
        .first()
    )
    if variante is None or not variante.sku:
        return None
    return variante


def nombre_es_muestra(producto_nombre_externo):
    """Única definición de línea de MUESTRA: trae ``producto_nombre_externo``.

    Null y ``""`` no cuentan; un nombre con sólo espacios sí. Se lee el nombre y no el
    flag ``requiere_produccion`` porque ese se desincroniza. Producción, ``programar``
    y el derivado de ``requiere_produccion`` usan esta misma regla.
    """
    return bool(producto_nombre_externo)


def es_muestra(pedido_detalle):
    return nombre_es_muestra(pedido_detalle.producto_nombre_externo)


def detalles_de_muestra():
    """``PedidoDetalle`` de muestra (misma regla que ``nombre_es_muestra``, en consulta)."""
    return PedidoDetalle.objects.exclude(producto_nombre_externo__isnull=True).exclude(
        producto_nombre_externo=""
    )


# Complemento sobre ``PedidoDetalleTalla``: tallas cuya línea NO es muestra.
TALLAS_DE_LINEA_NO_MUESTRA_Q = Q(pedido_detalle__producto_nombre_externo__isnull=True) | Q(
    pedido_detalle__producto_nombre_externo__exact=""
)


def cargar_variantes_produccion(pedido_detalles):
    """Deja en cada línea de muestra lo mismo que ``variantes_produccion_prefetch()``.

    Para renglones que llegan SIN ese prefetch (DRF lo vacía tras un PATCH; el
    alta no lo trae): UNA consulta para todos, en vez de una por línea. Con el
    prefetch presente no consulta nada.
    """
    pendientes = {
        detalle.pk: detalle
        for detalle in pedido_detalles
        if es_muestra(detalle) and not hasattr(detalle, VARIANTES_PRODUCCION_ATTR)
    }
    if not pendientes:
        return
    for detalle in pendientes.values():
        setattr(detalle, VARIANTES_PRODUCCION_ATTR, [])
    for variante in variantes_produccion_activas().filter(pedido_detalle_id__in=pendientes):
        getattr(pendientes[variante.pedido_detalle_id], VARIANTES_PRODUCCION_ATTR).append(variante)


def skus_produccion_por_talla(pedido_detalle):
    """``{talla_id: sku}`` de una línea de MUESTRA; ``{}`` si no lo es.

    Lee lo que dejó ``variantes_produccion_prefetch()`` o
    ``cargar_variantes_produccion()``; sin ninguno de los dos cae a una consulta
    por línea (nunca por talla).
    """
    if not es_muestra(pedido_detalle):
        return {}
    variantes = getattr(pedido_detalle, VARIANTES_PRODUCCION_ATTR, None)
    if variantes is None:
        variantes = variantes_produccion_activas().filter(pedido_detalle_id=pedido_detalle.pk)
    return {variante.talla_id: variante.sku for variante in variantes if variante.sku}
