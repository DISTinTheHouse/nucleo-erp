"""Costo unitario del producto terminado de una OP (#809).

Materiales: lo que la OP sacó de inventario (consumo automático y recálculos)
menos lo que regresó, al costo con el que se movió cada renglón, entre las piezas
de la OP. Más ``costo_maquila_unitario`` (confección externa o interna de LAZZAR).
Las entradas de producto terminado de la propia OP (recepciones) no cuentan.
"""

from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Sum

from inventarios.models import MovimientoInventarioDetalle, TipoMovimiento

CERO = Decimal("0")


def costo_unitario_produccion(op):
    """Costo por pieza en MXN, o ``None`` si no hay materiales costeados ni maquila."""
    importe = ExpressionWrapper(F("cantidad") * F("costo_unitario"), output_field=DecimalField(max_digits=30, decimal_places=8))
    filas = dict(
        MovimientoInventarioDetalle.objects.filter(
            movimiento_inventario__op_id=op.pk, movimiento_inventario__recepcion__isnull=True,
            movimiento_inventario__activo=True,
        )
        .values_list("movimiento_inventario__tipo_movimiento")
        .annotate(total=Sum(importe))
    )
    materiales = (filas.get(TipoMovimiento.SALIDA) or CERO) - (filas.get(TipoMovimiento.ENTRADA) or CERO)
    # Sólo renglones activos (EC-880): uno dado de baja no es pieza producida.
    piezas = op.orden_produccion_detalle.filter(activo=True).aggregate(t=Sum("cantidad"))["t"] or CERO
    maquila = op.costo_maquila_unitario or CERO
    if piezas <= 0:
        return maquila or None
    costo = max(materiales, CERO) / piezas + maquila
    return costo or None
