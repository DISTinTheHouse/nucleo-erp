"""Costeo de inventario por costo promedio ponderado (#809).

Una sola puerta: ``costear(empresa_id, renglones, origen)``. Cada flujo que escribe
``MovimientoInventarioDetalle`` la llama DESPUÉS de mover ``Existencia`` y de crear
los renglones, con el ``delta`` real de cada uno (positivo entra, negativo sale,
0 si no cambia la existencia de la empresa, como una transferencia).

Reglas:

- **Entrada valorada** (``costo`` dado: recepción de OC, producto terminado de una
  OP): el promedio se recalcula
  ``(existencia_previa × promedio + cantidad × costo) / (existencia_previa + cantidad)``
  y el cambio queda en ``HistorialCostoInventario``. Si el artículo nunca tuvo costo
  (promedio 0), el nuevo promedio es el de la entrada: la existencia anterior al
  costeo no lo diluye a 0.
- **Entrada sin valor** (ajuste a la alza, devolución, reintegro, entrada manual) y
  **salidas, ajustes a la baja y transferencias**: salen o entran al promedio
  vigente, que no cambia.
- El renglón guarda en ``costo_unitario`` el costo con el que se movió.

``existencia_previa`` sale de ``Existencia`` (suma en los almacenes de la empresa)
menos los deltas de este movimiento: siempre cuadra con el stock real, sin un
contador paralelo. Consultas fijas por movimiento, no por renglón.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from inventarios.models import CostoInventario, Existencia, HistorialCostoInventario, MovimientoInventarioDetalle

CERO = Decimal("0")
PRECISION_COSTO = Decimal("0.00000001")


class Origen:
    RECEPCION_OC = "RECEPCION_OC"
    PRODUCCION = "PRODUCCION"
    CONSUMO_OP = "CONSUMO_OP"
    PEDIDO = "PEDIDO"
    FACTURA = "FACTURA"
    OPERACION = "OPERACION"
    TRANSFERENCIA = "TRANSFERENCIA"


def clave(producto_id=None, producto_variante_id=None, variante_produccion_id=None):
    """Artículo al que se le lleva costo: el SKU de muestra, si no la variante, si no el producto."""
    if variante_produccion_id:
        return ("vp", variante_produccion_id)
    if producto_variante_id:
        return ("pv", producto_variante_id)
    if producto_id:
        return ("p", producto_id)
    return None


def _clave_de(obj):
    return clave(obj.producto_id, obj.producto_variante_id, obj.variante_produccion_id)


def _filtro(claves, prefijo=""):
    q = Q(pk__in=[])
    por_tipo = defaultdict(list)
    for tipo, pk in claves:
        por_tipo[tipo].append(pk)
    if por_tipo["vp"]:
        q |= Q(**{f"{prefijo}variante_produccion_id__in": por_tipo["vp"]})
    if por_tipo["pv"]:
        q |= Q(**{f"{prefijo}producto_variante_id__in": por_tipo["pv"], f"{prefijo}variante_produccion__isnull": True})
    if por_tipo["p"]:
        q |= Q(**{
            f"{prefijo}producto_id__in": por_tipo["p"],
            f"{prefijo}producto_variante__isnull": True,
            f"{prefijo}variante_produccion__isnull": True,
        })
    return q


def _existencias(empresa_id, claves):
    filas = (
        Existencia.objects.filter(_filtro(claves), almacen__empresa_id=empresa_id)
        .values("producto_id", "producto_variante_id", "variante_produccion_id")
        .annotate(total=Sum("cantidad"))
    )
    totales = defaultdict(lambda: CERO)
    for f in filas:
        totales[clave(f["producto_id"], f["producto_variante_id"], f["variante_produccion_id"])] += f["total"] or CERO
    return totales


def _costos(empresa_id, claves, bloquear=True):
    qs = CostoInventario.objects.filter(_filtro(claves), empresa_id=empresa_id)
    if bloquear:
        qs = qs.select_for_update()
    return {_clave_de(c): c for c in qs}


def costos_vigentes(empresa_id, claves):
    """``{clave: costo_promedio}`` sin bloquear; 0 si el artículo no tiene costo."""
    claves = {c for c in claves if c}
    if not claves:
        return {}
    costos = _costos(empresa_id, claves, bloquear=False)
    return {c: (costos[c].costo_promedio if c in costos else CERO) for c in claves}


def costos_promedio_al(llaves, fecha):
    """``{(empresa_id, producto_id, producto_variante_id): costo}`` vigente en ``fecha``.

    Costo promedio ponderado (#809) tal como estaba en ``fecha`` (EC-815): el
    ``costo_nuevo`` del último cambio hasta ``fecha``; si el primer cambio es
    posterior, su ``costo_anterior``; sin historial, el ``costo_promedio`` actual.
    Tres consultas fijas, sin importar cuántas llaves.
    """
    por_empresa = defaultdict(set)
    for empresa_id, producto_id, variante_id in llaves:
        k = clave(producto_id, variante_id)
        if empresa_id and k:
            por_empresa[empresa_id].add(k)
    if not por_empresa:
        return {}
    q = Q(pk__in=[])
    for empresa_id, claves in por_empresa.items():
        q |= _filtro(claves) & Q(empresa_id=empresa_id)
    costos = list(CostoInventario.objects.filter(q).only(
        "id", "empresa_id", "producto_id", "producto_variante_id", "variante_produccion_id", "costo_promedio",
    ))
    ids = [c.pk for c in costos]
    hasta = dict(
        HistorialCostoInventario.objects.filter(costo_inventario_id__in=ids, fecha__lte=fecha)
        .order_by("costo_inventario_id", "-fecha", "-id").distinct("costo_inventario_id")
        .values_list("costo_inventario_id", "costo_nuevo")
    )
    despues = dict(
        HistorialCostoInventario.objects.filter(costo_inventario_id__in=ids, fecha__gt=fecha)
        .exclude(costo_inventario_id__in=list(hasta))
        .order_by("costo_inventario_id", "fecha", "id").distinct("costo_inventario_id")
        .values_list("costo_inventario_id", "costo_anterior")
    ) if len(hasta) < len(ids) else {}
    por_clave = {
        (c.empresa_id, _clave_de(c)): hasta.get(c.pk, despues.get(c.pk, c.costo_promedio)) or CERO
        for c in costos
    }
    return {
        (empresa_id, producto_id, variante_id): por_clave.get((empresa_id, clave(producto_id, variante_id)), CERO)
        for empresa_id, producto_id, variante_id in llaves
    }


def costear(empresa_id, renglones, origen):
    """``renglones``: ``[(detalle, delta, costo_entrada_o_None), ...]``. Llamar dentro
    de la transacción del flujo. Guarda ``costo_unitario`` en cada detalle."""
    renglones = [(d, Decimal(str(delta or 0)), costo) for d, delta, costo in renglones if _clave_de(d)]
    if not renglones or not empresa_id:
        return
    claves = {_clave_de(d) for d, _, _ in renglones}
    costos = _costos(empresa_id, claves)
    faltan = claves - costos.keys()
    if faltan:
        # Alta en bloque (#1025): antes, un ``get_or_create`` por artículo sin costo.
        # ``ignore_conflicts`` cubre la carrera con otra transacción; luego se releen
        # bloqueadas, como las que ya existían.
        campos = {"vp": "variante_produccion_id", "pv": "producto_variante_id", "p": "producto_id"}
        CostoInventario.objects.bulk_create(
            [CostoInventario(empresa_id=empresa_id, **{campos[tipo]: pk}) for tipo, pk in faltan],
            ignore_conflicts=True,
        )
        costos.update(_costos(empresa_id, faltan))
    # Existencia previa al movimiento = la de ahora menos lo que este movimiento sumó.
    existencia = _existencias(empresa_id, claves)
    for d, delta, _ in renglones:
        existencia[_clave_de(d)] -= delta

    cambiados, historial = {}, []
    for d, delta, costo_entrada in renglones:
        k = _clave_de(d)
        costo = costos[k]
        previa = existencia[k]
        anterior = costo.costo_promedio or CERO
        if delta > 0 and costo_entrada:  # costo 0 o desconocido: entra sin valor
            costo_entrada = Decimal(str(costo_entrada)).quantize(PRECISION_COSTO)
            base = max(previa, CERO)
            if anterior == CERO or base == CERO:
                nuevo = costo_entrada
            else:
                nuevo = ((base * anterior + delta * costo_entrada) / (base + delta)).quantize(PRECISION_COSTO)
            d.costo_unitario = costo_entrada
            if nuevo != anterior:
                costo.costo_promedio = nuevo
                costo.actualizado_en = timezone.now()
                cambiados[k] = costo
                historial.append(HistorialCostoInventario(
                    costo_inventario=costo, origen=origen, costo_anterior=anterior, costo_nuevo=nuevo,
                    existencia_previa=previa, cantidad_entrada=delta, costo_entrada=costo_entrada,
                    movimiento_detalle=d,
                ))
        else:
            d.costo_unitario = anterior
        existencia[k] = previa + delta

    if cambiados:
        CostoInventario.objects.bulk_update(cambiados.values(), ["costo_promedio", "actualizado_en"])
    if historial:
        HistorialCostoInventario.objects.bulk_create(historial)
    MovimientoInventarioDetalle.objects.bulk_update([d for d, _, _ in renglones], ["costo_unitario"])
