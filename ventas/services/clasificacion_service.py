from datetime import timedelta

from django.utils import timezone

# Días calendario desde la fecha base del pedido (ver ``fecha_base_compromiso``),
# según ``Pedido.Clasificacion``.
# ``X`` (Solo para facturar) no tiene compromiso de entrega -> sin rango.
_RANGOS_DIAS = {
    "A": (2, 5),
    "B": (5, 8),
    "C": (5, 15),
    "D": (4 * 7, 6 * 7),
    "E": (6 * 7, 8 * 7),
    "F": (8 * 7, 10 * 7),
}


def fecha_base_compromiso(pedido):
    """Día (hora local) desde el que corre el compromiso de entrega.

    ``fecha_confirmacion`` (la sella mesa de control al confirmar el pedido); sin
    ella, ``created_at``. ``None`` si no hay ninguna de las dos.
    """
    base = pedido.fecha_confirmacion or pedido.created_at
    return timezone.localdate(base) if base else None


def rango_fecha_entrega(pedido):
    """``(fecha_min, fecha_max)`` estimadas a partir de ``pedido.clasificacion``,
    contando desde ``fecha_base_compromiso(pedido)``.

    ``(None, None)`` si el pedido no tiene clasificación asignada o es ``X``.
    """
    rango = _RANGOS_DIAS.get(pedido.clasificacion)
    base = fecha_base_compromiso(pedido)
    if not rango or base is None:
        return None, None
    dias_min, dias_max = rango
    return base + timedelta(days=dias_min), base + timedelta(days=dias_max)
