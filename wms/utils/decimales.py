from decimal import Decimal


def normalizar_decimal(value):
    """Convierte ``value`` a ``Decimal``, tratando ``None``/``""`` como cero.

    Único punto de conversión: antes ``ExistenciaService``, ``TransferenciaService``,
    ``ReservaInventarioService``, ``PickingService`` y ``PackingService`` redefinían
    el mismo cuerpo (``Decimal(str(value or "0"))``) cada uno bajo su propio nombre
    (``_normalize``/``_normalize_quantity``).
    """
    return Decimal(str(value or "0"))
