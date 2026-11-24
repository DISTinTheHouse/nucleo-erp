"""Retención de pedidos por mesa de control.

Un pedido retenido (crédito, cliente pidió esperar, cambio por confirmar) no se
cancela: se congela. Mientras esté retenido nadie inicia trabajo nuevo sobre él:

- almacén: no se surte (picking), no se empaca (packing) ni se despacha;
- producción: no se crean OP, OB, OR ni OCM ligadas al pedido.

Lo que ya está en manos de los operadores (avances de órdenes abiertas, pickings
ya creados) no se detiene aquí: se cancela con su propio endpoint si hace falta.
"""

from rest_framework.exceptions import ValidationError


def mensaje_retenido(pedido):
    motivo = f": {pedido.motivo_retencion}" if pedido.motivo_retencion else ""
    return (
        f"El pedido {pedido.folio or pedido.pk} está retenido por mesa de control{motivo}. "
        "Pide a mesa de control que lo libere."
    )


def exigir_no_retenido(pedido, campo="pedido"):
    """Sin consultas: ``retenido`` viene en la fila del pedido ya cargada."""
    if pedido is not None and pedido.retenido:
        raise ValidationError({campo: mensaje_retenido(pedido)})
