"""Resumen de consumos de una OP por material: planeado, ajustes y actual (EC-880).

Una sola regla para el detalle de la OP (``consumos_resumen``) y para
``RecalculoConsumoTelaService.recalcular``:

- Los renglones (``ConsumoProduccionDetalle``) se ordenan por
  ``(consumo_produccion_id, consumo_detalle_id)``: el orden de alta.
- **Consumo inicial** = el primer encabezado de la OP que tiene renglones. Lo
  crea ``OrdenProduccionService.save_orden_produccion`` en la misma transacción
  que la OP, con un renglón por material; los recálculos sólo agregan
  encabezados posteriores con la diferencia (con signo). Los encabezados vacíos
  (``POST /produccion/consumo/``) no tienen renglones y no cuentan.
- **Planeado** = la cantidad del material en el consumo inicial; 0 si el
  material no está en él.
- **Actual** = la suma de todos los renglones del material.
- **Ajustes** = actual - planeado.
"""

from decimal import Decimal, ROUND_HALF_UP

QTY_PRECISION = Decimal("0.0001")
CERO = Decimal("0")


def _q(valor):
    return Decimal(str(valor or 0)).quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)


def resumen_consumos(renglones):
    """Lista de ``{producto_id, renglon, planeado, ajustes, actual}`` (``Decimal``
    a 4 decimales), en el orden en que aparece cada material. ``renglones``:
    TODOS los ``ConsumoProduccionDetalle`` de UNA OP (sin ellos no se sabe cuál
    es el consumo inicial), en cualquier orden: se reordenan aquí. ``renglon`` es
    el primero del material; aquí no se lee su ``producto`` (no hay consultas)."""
    filas = sorted(renglones, key=lambda d: (d.consumo_produccion_id, d.consumo_detalle_id))
    if not filas:
        return []
    inicial = filas[0].consumo_produccion_id
    por_producto = {}
    for fila in filas:
        dato = por_producto.get(fila.producto_id)
        if dato is None:
            dato = por_producto[fila.producto_id] = {
                "producto_id": fila.producto_id,
                "renglon": fila,
                "planeado": CERO,
                "actual": CERO,
            }
        cantidad = _q(fila.cantidad)
        dato["actual"] += cantidad
        if fila.consumo_produccion_id == inicial:
            dato["planeado"] += cantidad
    resumen = []
    for dato in por_producto.values():
        dato["planeado"] = _q(dato["planeado"])
        dato["actual"] = _q(dato["actual"])
        dato["ajustes"] = _q(dato["actual"] - dato["planeado"])
        resumen.append(dato)
    return resumen


def resumen_consumos_api(renglones):
    """``consumos_resumen`` del detalle de la OP: cantidades como texto con 4
    decimales (mismo formato que ``consumos[].detalles[].cantidad``) y
    ``unidad`` = clave de ``Producto.unidad_medida`` en mayúsculas (como
    ``productos[].habilitacion[].unidad``), ``None`` si el material no tiene."""
    salida = []
    for dato in resumen_consumos(renglones):
        producto = dato["renglon"].producto
        unidad = getattr(producto, "unidad_medida", None)
        salida.append({
            "producto": dato["producto_id"],
            "producto_nombre": getattr(producto, "nombre", None),
            "unidad": unidad.clave.upper() if unidad is not None and unidad.clave else None,
            "planeado": str(dato["planeado"]),
            "ajustes": str(dato["ajustes"]),
            "actual": str(dato["actual"]),
        })
    return salida
