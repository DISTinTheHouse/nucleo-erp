"""Historial (kardex valorizado) de un SKU: ``movimientos/historial-sku/``.

Sobre el mismo cálculo del auxiliar (``auxiliar.kardex``: saldo corrido por
almacén, ajustes por diferencia, stock cargado sin movimiento) agrega lo que
contabilidad y WMS piden: cabecera del artículo, saldo inicial y final en
piezas y en pesos, origen y folio de cada renglón, ubicación y valor corrido.

Valor: costo promedio ponderado oficial (#809). El saldo inicial se valúa al
costo vigente al inicio del rango; cada renglón, al costo vigente después de
él (``HistorialCostoInventario`` ligado a su renglón); el saldo final, al costo
vigente al cierre. Consultas fijas, sin importar cuántos movimientos tenga el SKU.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from inventarios.models import CostoInventario, HistorialCostoInventario, Ubicacion
from inventarios.services.costeo import costos_promedio_al, costos_vigentes

from . import auxiliar
from .reportes_periodo import almacen_ids_movimientos, id_positivo, parse_fechas

CERO = Decimal("0")
Q4 = Decimal("0.0001")
Q2 = Decimal("0.01")


def _d(valor):
    return Decimal(str(valor or 0))


def _ubicacion_codigo(u):
    partes = [p for p in (u["pasillo"], u["rack"], u["nivel"], u["posicion"]) if p]
    return "-".join(partes) or str(u["id_ubicacion"])


def historial_sku(user, query_params):
    """Historial completo (sin paginar) de un SKU. 400 sin ``sku``; 404 si el SKU
    no es de la empresa activa del usuario."""
    qp = query_params
    sku = (qp.get("sku") or "").strip()
    if not sku:
        raise ValidationError({"sku": "sku es requerido."})
    almacen_id = id_positivo(qp.get("almacen_id") or qp.get("almacen"), "almacen_id")
    inicio_dt = final_dt = fecha_inicio = fecha_final = None
    if qp.get("fecha_inicio") or qp.get("fecha_final"):
        fecha_inicio, fecha_final, inicio_dt, final_dt = parse_fechas(qp)

    variante = (
        auxiliar.variantes_scope(user)
        .filter(sku=sku)
        .values("id", "sku", "nombre", "producto_id", "producto__nombre", "color__nombre", "talla__nombre", "activo")
        .first()
    )
    if not variante:
        raise NotFound("SKU no encontrado en la empresa.")

    # Mismo alcance que el auxiliar: empresa + sucursales; el superusuario, su empresa activa.
    alcance = None
    if not user.is_superuser:
        almacenes, _ = almacen_ids_movimientos(user, qp)
        alcance = (almacenes, user.sucursales_permitidas())
    data = auxiliar.kardex(
        user, variante["id"], almacen_id, inicio_dt, final_dt, alcance, limite=auxiliar.LIMITE_RENGLONES_MAX * 1000,
    )
    filas = data["results"]
    resumen = data["resumen"]

    # Costos oficiales: vigente hoy, al inicio y al cierre del rango, y sus cambios
    # por renglón (fijo: costos_vigentes 1, costos_promedio_al 3 x 2, historial 1).
    empresa_id = user.empresa_id
    clave = ("pv", variante["id"])
    costo_actual = costos_vigentes(empresa_id, {clave}).get(clave, CERO)
    llave = (empresa_id, variante["producto_id"], variante["id"])
    costo_inicial = (
        costos_promedio_al({llave}, inicio_dt - timedelta(microseconds=1)).get(llave, CERO) if inicio_dt else CERO
    )
    costo_final = costos_promedio_al({llave}, final_dt).get(llave, CERO) if final_dt else costo_actual
    cambios = dict(
        HistorialCostoInventario.objects.filter(
            costo_inventario__in=CostoInventario.objects.filter(empresa_id=empresa_id, producto_variante_id=variante["id"]),
            movimiento_detalle_id__in=[f["movimiento_detalle_id"] for f in filas],
        ).values_list("movimiento_detalle_id", "costo_nuevo")
    ) if filas else {}
    ub_ids = {f["ubicacion_id"] for f in filas if f["ubicacion_id"]}
    ubicaciones = {
        u["id_ubicacion"]: _ubicacion_codigo(u)
        for u in Ubicacion.objects.filter(pk__in=ub_ids).values("id_ubicacion", "pasillo", "rack", "nivel", "posicion")
    } if ub_ids else {}

    saldo_inicial = _d(resumen["saldo_inicial"])
    costo = costo_inicial if inicio_dt else (
        # Sin rango: el historial arranca en el primer movimiento; costo del primer cambio.
        _d(next((cambios[f["movimiento_detalle_id"]] for f in filas if f["movimiento_detalle_id"] in cambios), costo_actual))
    )
    movimientos = []
    total_entradas_valor = total_salidas_valor = CERO
    for f in filas:
        costo = _d(cambios.get(f["movimiento_detalle_id"], costo))
        entrada = _d(f["entradas"])
        salida = _d(f["salidas"])
        costo_mov = _d(f["costo_unitario"]) or costo
        entrada_valor = (entrada * costo_mov).quantize(Q2)
        salida_valor = (salida * costo_mov).quantize(Q2)
        total_entradas_valor += entrada_valor
        total_salidas_valor += salida_valor
        saldo = _d(f["saldo"])
        movimientos.append({
            "movimiento_inventario_id": f["movimiento_inventario_id"],
            "movimiento_detalle_id": f["movimiento_detalle_id"],
            "fecha": f["fecha"],
            "tipo_movimiento": f["tm"],
            "efecto": f["efecto"],
            "origen_tipo": f["origen_tipo"],
            "origen_folio": f["origen_folio"],
            "origen_id": f["origen_id"],
            "almacen_id": f["almacen_id"],
            "almacen_codigo": f["almacen_codigo"],
            "almacen_nombre": f["almacen_nombre"],
            "ubicacion_id": f["ubicacion_id"],
            "ubicacion_codigo": ubicaciones.get(f["ubicacion_id"]),
            "entrada": f["entradas"],
            "salida": f["salidas"],
            "costo_unitario": str(costo_mov.quantize(Q2)),
            "entrada_valor": str(entrada_valor),
            "salida_valor": str(salida_valor),
            "saldo": f["saldo"],
            "saldo_almacen": f["saldo_almacen"],
            "costo_promedio": str(costo.quantize(Q2)),
            "saldo_valor": str((saldo * costo).quantize(Q2)),
            "usuario": f["usuario"],
            "notas": f["observaciones"],
        })

    saldo_final = _d(resumen["saldo_final"])
    existencia = _d(resumen["existencia_actual"])
    hasta_hoy = final_dt is None or final_dt >= timezone.now()
    return {
        "sku": variante["sku"],
        "producto": {
            "producto_id": variante["producto_id"],
            "producto_variante_id": variante["id"],
            "sku": variante["sku"],
            "descripcion": variante["nombre"] or variante["producto__nombre"],
            "producto_nombre": variante["producto__nombre"],
            "color": variante["color__nombre"],
            "talla": variante["talla__nombre"],
            "activo": variante["activo"],
            "costo_promedio_actual": str(_d(costo_actual).quantize(Q2)),
        },
        "fecha_inicio": fecha_inicio,
        "fecha_final": fecha_final,
        "almacen_id": almacen_id,
        "saldo_inicial": {
            "fecha": (fecha_inicio - timedelta(days=1)) if fecha_inicio else None,
            "piezas": str(saldo_inicial.quantize(Q4)),
            "costo_unitario": str(_d(costo_inicial).quantize(Q2)),
            "valor": str((saldo_inicial * _d(costo_inicial)).quantize(Q2)),
        },
        "totales": {
            "entradas": resumen["total_entradas"],
            "salidas": resumen["total_salidas"],
            "entradas_valor": str(total_entradas_valor),
            "salidas_valor": str(total_salidas_valor),
            "registros": len(movimientos),
        },
        "saldo_final": {
            "fecha": fecha_final,
            "piezas": str(saldo_final.quantize(Q4)),
            "costo_unitario": str(_d(costo_final).quantize(Q2)),
            "valor": str((saldo_final * _d(costo_final)).quantize(Q2)),
            # Existencia real hoy (``Existencia``) y si el saldo final cuadra con ella
            # (solo aplica cuando el rango llega a hoy).
            "existencia_actual": str(existencia.quantize(Q4)),
            "cuadra_con_existencia": (saldo_final == existencia) if hasta_hoy else None,
        },
        "results": movimientos,
    }
