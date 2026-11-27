"""Valuación del inventario al cierre: ``existencias/valuacion-cierre/``.

La foto del inventario físico a una fecha: existencia por almacén y artículo
al final de ``fecha`` (hora de México), con su costo promedio ponderado
vigente a esa hora (#809) y el total. Es el reporte de existencias por periodo
con ``fecha_inicio = fecha_final = fecha`` (mismo alcance, misma reconstrucción
desde ``Existencia`` hacia atrás, mismas consultas fijas), sin los renglones en
cero.
"""
from decimal import Decimal

from django.http import QueryDict
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .reportes_periodo import _fecha, quantize_money, quantize_qty, reporte_existencias, to_decimal


def fecha_de_corte(query_params):
    """``fecha`` (YYYY-MM-DD); por defecto, hoy. No puede ser futura."""
    if not (query_params.get("fecha") or "").strip():
        return timezone.localdate()
    fecha = _fecha(query_params, "fecha")
    if fecha > timezone.localdate():
        raise ValidationError({"fecha": "fecha no puede ser futura."})
    return fecha


def valuacion_cierre(user, query_params, almacen_id=None):
    fecha = fecha_de_corte(query_params)
    qp = QueryDict(mutable=True)
    for clave in ("almacen", "almacen_id", "empresa", "empresa_id", "sucursal", "sucursal_id",
                  "producto", "producto_id", "producto_variante", "producto_variante_id"):
        if query_params.get(clave):
            qp[clave] = query_params.get(clave)
    qp["fecha_inicio"] = qp["fecha_final"] = fecha.isoformat()
    if almacen_id is not None:
        rep = reporte_existencias(user, qp, almacen_id)
    else:
        rep = reporte_existencias(user, qp)

    filas = []
    por_almacen = {}
    total_piezas = Decimal("0")
    total_valor = Decimal("0")
    for f in rep["results"]:
        piezas = to_decimal(f["existencia_final"])
        if not piezas:
            continue
        valor = to_decimal(f["costo_existencia_final"])
        filas.append({
            "almacen_id": f["almacen_id"],
            "almacen_codigo": f["almacen_codigo"],
            "almacen_nombre": f["almacen_nombre"],
            "producto_id": f["producto_id"],
            "producto_variante_id": f["producto_variante_id"],
            "sku": f["sku"],
            "producto_nombre": f["producto_nombre"],
            "color": f["color"],
            "talla": f["talla"],
            "existencia": f["existencia_final"],
            "costo_unitario": f["costo_unitario_final"],
            "costo_total": f["costo_existencia_final"],
        })
        a = por_almacen.setdefault(f["almacen_id"], {
            "almacen_id": f["almacen_id"], "almacen_codigo": f["almacen_codigo"], "almacen_nombre": f["almacen_nombre"],
            "articulos": 0, "piezas": Decimal("0"), "valor": Decimal("0"),
        })
        a["articulos"] += 1
        a["piezas"] += piezas
        a["valor"] += valor
        total_piezas += piezas
        total_valor += valor

    return {
        "fecha": fecha,
        "almacen_id": rep["almacen_id"],
        "almacenes": rep["almacenes"],
        "sin_almacenes": rep["sin_almacenes"],
        "results": filas,
        "resumen": {
            "articulos": len(filas),
            "piezas": str(quantize_qty(total_piezas)),
            "valor": str(quantize_money(total_valor)),
        },
        "resumen_por_almacen": [
            {**a, "piezas": str(quantize_qty(a["piezas"])), "valor": str(quantize_money(a["valor"]))}
            for _, a in sorted(por_almacen.items())
        ],
    }
