"""Montos de ``FacturaProveedor``: el encabezado se deriva de sus líneas (#385).

Convención (la misma que valida el resto de Finanzas): por línea,
``total = subtotal - descuento + impuesto``; el encabezado suma las líneas
(``impuestos`` = suma de ``impuesto``). Un monto de encabezado que no cuadra
con sus líneas se rechaza, no se guarda tal cual.
"""

from decimal import Decimal

CENTAVO = Decimal("0.01")
CERO = Decimal("0")


def _d(valor):
    return Decimal(str(valor if valor is not None else 0))


def completar_linea(cantidad, precio_unitario, descuento, impuesto, subtotal=None, total=None):
    """``(subtotal, total)`` de una línea; lo no capturado (``None``) se deriva.

    - Sin ``subtotal`` ni ``total``: ``subtotal = cantidad × precio_unitario`` y
      ``total = subtotal - descuento + impuesto``.
    - Solo ``total``: ``subtotal = total + descuento - impuesto``.
    - Solo ``subtotal``: ``total = subtotal - descuento + impuesto``.
    - Ambos: el ``total`` debe cuadrar, si no levanta ``ValueError``.
    """
    descuento, impuesto = _d(descuento), _d(impuesto)
    if subtotal is None and total is not None:
        return _d(total) + descuento - impuesto, _d(total)
    subtotal = _d(cantidad) * _d(precio_unitario) if subtotal is None else _d(subtotal)
    esperado = subtotal - descuento + impuesto
    if total is None:
        return subtotal, esperado
    total = _d(total)
    if abs(total - esperado) > CENTAVO:
        raise ValueError(f"total {total} != subtotal - descuento + impuesto ({esperado})")
    return subtotal, total


def totales_desde_lineas(lineas):
    """Encabezado a partir de líneas ya completas (dicts o instancias)."""
    def campo(linea, nombre):
        return _d(linea[nombre] if isinstance(linea, dict) else getattr(linea, nombre))

    lineas = list(lineas)
    return {
        "subtotal": sum((campo(l, "subtotal") for l in lineas), CERO),
        "descuento": sum((campo(l, "descuento") for l in lineas), CERO),
        "impuestos": sum((campo(l, "impuesto") for l in lineas), CERO),
        "total": sum((campo(l, "total") for l in lineas), CERO),
    }


def encabezado_cuadra(encabezado, derivado):
    """Campos del ``encabezado`` (dict, solo los capturados) que no cuadran con ``derivado``."""
    return [
        nombre for nombre, valor in encabezado.items()
        if nombre in derivado and abs(_d(valor) - derivado[nombre]) > CENTAVO
    ]


def recalcular_encabezado(factura):
    """Reescribe los montos del encabezado desde las líneas guardadas."""
    montos = totales_desde_lineas(factura.factura_proveedor_detalles.all())
    for nombre, valor in montos.items():
        setattr(factura, nombre, valor)
    factura.save(update_fields=list(montos))
    return montos
