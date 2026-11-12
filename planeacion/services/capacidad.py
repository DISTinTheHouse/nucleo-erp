"""Capacidad de fabricación de producto terminado con la materia prima en existencia.

Usa el BOM activo de cada variante y la misma regla que el consumo real de una OP
(``OrdenProduccionService``): por pieza se requiere ``cantidad × (1 + desperdicio/100)``
del componente, y su existencia es la suma de ``Existencia`` del producto componente
(directa o de cualquiera de sus variantes) en los almacenes de la empresa.

Igual que la OP, **no convierte unidades**: si el BOM pide ``cm`` y el componente se
inventaría en ``m`` el cálculo sale mal en ambos lados. También como la OP, cuentan
todos los insumos activos del BOM; ``obligatorio`` solo se informa. Los renglones con unidad distinta se marcan con
``unidad_inconsistente`` para corregir el BOM o el catálogo.

``fabricable`` es lo que alcanza para ESA variante sola: dos SKUs que comparten tela
no pueden fabricar los dos su máximo a la vez. Para el total de un resurtido usar
``necesidades`` (consolida por componente).

Dos consultas fijas, sin importar cuántas variantes.
"""

import math
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import Coalesce

from inventarios.models import Existencia
from produccion.models import BomDetalle

CERO = Decimal("0")
CIEN = Decimal("100")
CUATRO_DECIMALES = Decimal("0.0001")


def materiales(empresa, ids_variante, sucursales=None):
    """``{variante_id: {"bom_id", "materiales": [...]}}`` para las variantes con BOM
    activo con insumos activos. ``ids_variante``: lista o subconsulta de ids.
    ``sucursales``: ids de sucursal a considerar para la existencia (``None`` = toda la empresa)."""
    renglones = list(
        BomDetalle.objects.filter(
            activo=True, componente__isnull=False,
            bom__empresa=empresa, bom__activo=True, bom__producto_variante_id__in=ids_variante,
        ).values(
            "bom_id", "bom__producto_variante_id", "bom__version", "componente_id", "componente__nombre",
            "componente__unidad_medida_id", "componente__unidad_medida__clave", "unidad_id", "unidad__clave",
            "cantidad", "desperdicio", "obligatorio",
        ).order_by("bom__producto_variante_id", "-bom__version", "bom_id")
    )
    componentes = {r["componente_id"] for r in renglones}
    existencia = defaultdict(lambda: CERO)
    if componentes:
        qs = Existencia.objects.filter(almacen__empresa=empresa).annotate(
            comp=Coalesce("producto_id", "producto_variante__producto_id"),
        ).filter(comp__in=componentes)
        if sucursales is not None:
            qs = qs.filter(almacen__sucursal_id__in=sucursales)
        for r in qs.values("comp").annotate(total=Sum("cantidad")):
            existencia[r["comp"]] += r["total"] or CERO

    por_variante = {}
    for r in renglones:
        v = r["bom__producto_variante_id"]
        actual = por_variante.get(v)
        if actual is None:
            actual = por_variante[v] = {"bom_id": r["bom_id"], "materiales": []}
        elif actual["bom_id"] != r["bom_id"]:
            continue  # más de un BOM activo: se usa la versión más alta
        por_pieza = (r["cantidad"] or CERO) * (1 + (r["desperdicio"] or CERO) / CIEN)
        hay = existencia[r["componente_id"]]
        actual["materiales"].append({
            "componente_id": r["componente_id"],
            "componente_nombre": r["componente__nombre"],
            "unidad_bom": r["unidad__clave"],
            "unidad_inventario": r["componente__unidad_medida__clave"],
            "unidad_inconsistente": r["unidad_id"] != r["componente__unidad_medida_id"],
            "obligatorio": r["obligatorio"],
            "por_pieza": por_pieza,
            "existencia": hay,
            "alcanza_para": math.floor(hay / por_pieza) if por_pieza > 0 else None,
        })
    return por_variante


def resumen_variante(bom):
    """Capacidad de una variante a partir de su entrada de ``materiales``."""
    if not bom:
        return {"tiene_bom": False, "fabricable_con_existencia": None, "limitante": None, "bom_con_alertas": False}
    obligatorios = [m for m in bom["materiales"] if m["alcanza_para"] is not None]
    limitante = min(obligatorios, key=lambda m: m["alcanza_para"]) if obligatorios else None
    return {
        "tiene_bom": True,
        "fabricable_con_existencia": limitante["alcanza_para"] if limitante else None,
        "limitante": (
            {"componente_id": limitante["componente_id"], "componente_nombre": limitante["componente_nombre"]}
            if limitante else None
        ),
        "bom_con_alertas": any(m["unidad_inconsistente"] or m["por_pieza"] <= 0 for m in bom["materiales"]),
    }


def necesidades(boms, cantidades):
    """Consolida por componente lo que se necesita para fabricar ``cantidades``
    (``{variante_id: piezas}``). Devuelve ``(componentes, sin_bom)``."""
    total = {}
    sin_bom = []
    for variante_id, piezas in cantidades.items():
        bom = boms.get(variante_id)
        if not bom:
            sin_bom.append(variante_id)
            continue
        for m in bom["materiales"]:
            c = total.setdefault(m["componente_id"], {
                "componente_id": m["componente_id"], "componente_nombre": m["componente_nombre"],
                "unidad_inventario": m["unidad_inventario"], "unidades_bom": set(),
                "unidad_inconsistente": False, "requerido": CERO, "existencia": m["existencia"], "variantes": 0,
            })
            c["requerido"] += m["por_pieza"] * piezas
            c["unidades_bom"].add(m["unidad_bom"])
            c["unidad_inconsistente"] = c["unidad_inconsistente"] or m["unidad_inconsistente"]
            c["variantes"] += 1
    componentes = []
    for c in total.values():
        c["unidades_bom"] = sorted(u for u in c["unidades_bom"] if u)
        c["requerido"] = c["requerido"].quantize(CUATRO_DECIMALES)
        c["faltante"] = max(CERO, c["requerido"] - c["existencia"])
        c["alcanza"] = c["faltante"] == 0
        componentes.append(c)
    componentes.sort(key=lambda c: (c["alcanza"], -c["faltante"], c["componente_nombre"] or ""))
    return componentes, sin_bom
