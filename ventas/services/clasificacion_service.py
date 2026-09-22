from datetime import timedelta

# Días calendario desde ``Pedido.created_at``, según ``Pedido.Clasificacion``.
# ``X`` (Solo para facturar) no tiene compromiso de entrega -> sin rango.
_RANGOS_DIAS = {
    "A": (2, 5),
    "B": (5, 8),
    "C": (5, 15),
    "D": (4 * 7, 6 * 7),
    "E": (6 * 7, 8 * 7),
    "F": (8 * 7, 10 * 7),
}


def rango_fecha_entrega(pedido):
    """``(fecha_min, fecha_max)`` estimadas a partir de ``pedido.clasificacion``,
    contando desde ``pedido.created_at`` (fecha de alta del pedido).

    ``(None, None)`` si el pedido no tiene clasificación asignada o es ``X``.
    """
    rango = _RANGOS_DIAS.get(pedido.clasificacion)
    if not rango or not pedido.created_at:
        return None, None
    base = pedido.created_at.date()
    dias_min, dias_max = rango
    return base + timedelta(days=dias_min), base + timedelta(days=dias_max)
