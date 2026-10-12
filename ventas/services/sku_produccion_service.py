"""SKU de producción (``catalogo.VarianteProductoProduccion``) de las tallas de un pedido.

Única definición de "esta talla ya tiene SKU de producción" en ventas: variante
ACTIVA de esa (``pedido_detalle``, ``talla``) y con ``sku`` no vacío. La usan el
campo ``sku_produccion`` del detalle del pedido y ``vincular-sku-muestra``, para
que lo que el detalle anuncia como vinculable sea exactamente lo que el clic copia.
"""

from django.db.models import Prefetch

from catalogo.models import VarianteProductoProduccion

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


def skus_produccion_por_talla(pedido_detalle):
    """``{talla_id: sku}`` de una línea de MUESTRA; ``{}`` si no lo es.

    Lee lo que dejó ``variantes_produccion_prefetch()``; sin ese prefetch cae a
    una consulta por línea (nunca por talla).
    """
    if not (pedido_detalle.producto_nombre_externo or "").strip():
        return {}
    variantes = getattr(pedido_detalle, VARIANTES_PRODUCCION_ATTR, None)
    if variantes is None:
        variantes = variantes_produccion_activas().filter(pedido_detalle_id=pedido_detalle.pk)
    return {variante.talla_id: variante.sku for variante in variantes if variante.sku}
