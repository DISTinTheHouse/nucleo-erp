from decimal import ROUND_HALF_UP, Decimal

CENTAVO = Decimal("0.01")


def a_mxn(importe, tipo_cambio):
    """``importe`` en pesos, redondeado a centavos (EC-581)."""
    return (Decimal(importe or 0) * Decimal(tipo_cambio or 1)).quantize(CENTAVO, rounding=ROUND_HALF_UP)
