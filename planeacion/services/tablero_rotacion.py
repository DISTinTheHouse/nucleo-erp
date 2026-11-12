"""Tablero de rotación de producto terminado para la junta semanal de planeación.

Por artículo (variante, o producto con ``agrupar=producto``) junta en memoria:

- **Venta**: piezas de pedidos de VENTA autorizados o en proceso (no muestras, ni
  pedidos de error, ni cancelados). Fecha de la venta = confirmación del pedido, o
  su alta si no tiene. Periodo = últimas ``semanas``; periodo anterior = las
  ``semanas`` previas, para la tendencia.
- **Existencia**: suma de ``Existencia`` en los almacenes de la empresa (o del ``ambito``).
- **En proceso**: piezas ordenadas en OPs abiertas (pendiente, preparación, en
  producción, revisión o detenida).

Sobre eso calcula venta semanal, cobertura, clase ABC (Pareto por piezas del
periodo: A hasta 80 %, B hasta 95 %, C el resto y lo que no se vendió), resurtido
sugerido, un ``estado`` para decidir y cuánto se puede fabricar con la materia
prima en existencia según el BOM (``planeacion.services.capacidad``). Consultas fijas (7) sin importar cuántos
artículos haya: todo va agrupado en la base y se cruza en Python.
"""

import math
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, F, Max, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from catalogo.models import ProductoVariante
from inventarios.models import Existencia
from planeacion.services import capacidad
from produccion.models import OrdenProduccion, OrdenProduccionDetalle
from ventas.models import TIPO_PEDIDO_VENTA, Pedido, PedidoDetalleTalla

CODIGO_PRODUCTO_TERMINADO = "PT"
ESTATUS_PEDIDO_VENDIDO = (Pedido.ESTATUS_AUTORIZADA, Pedido.ESTATUS_EN_PROCESO)
E = OrdenProduccion.EstatusOrdenProduccion
ESTATUS_OP_ABIERTA = (E.PENDIENTE, E.PREPARACION, E.BORDANDO, E.REVISION, E.DETENIDO)

RESURTIR = "RESURTIR"
SOBREINVENTARIO = "SOBREINVENTARIO"
LENTO = "LENTO"
SIN_VENTA = "SIN_VENTA"
OK = "OK"
ESTADOS = (RESURTIR, SOBREINVENTARIO, LENTO, SIN_VENTA, OK)

ORDENAMIENTOS = {
    "vendido_periodo", "venta_semanal", "tendencia_pct", "existencia", "en_proceso", "disponible",
    "cobertura_semanas", "resurtido_sugerido", "dias_sin_venta", "ultima_venta", "importe_vendido",
    "nombre", "sku", "clase_abc", "fabricable_con_existencia",
}
CERO = Decimal("0")


def _num(valor):
    return Decimal(valor or 0)


def _fila_vacia():
    return {"vendido": CERO, "anterior": CERO, "importe": defaultdict(lambda: CERO), "serie": None, "ultima": None,
            "pedidos": 0, "clientes": 0, "existencia": CERO, "en_proceso": CERO}


def calcular(empresa, p, ambito=None):
    """``p``: parámetros ya validados (ver ``TableroRotacionParamsSerializer``).

    ``ambito``: ids de sucursal que acotan venta, existencia, en proceso y capacidad por igual
    (``?sucursal=`` o las sucursales permitidas del usuario); ``None`` = toda la empresa."""
    semanas, objetivo = p["semanas"], p["semanas_objetivo"]
    por_producto = p["agrupar"] == "producto"
    hoy = timezone.localdate()
    ahora = timezone.now()
    inicio = ahora - timedelta(weeks=semanas)
    inicio_anterior = ahora - timedelta(weeks=2 * semanas)

    # 1) Universo: variantes activas de producto terminado activo de la empresa.
    variantes = ProductoVariante.objects.filter(
        empresa=empresa, activo=True, producto__activo=True, producto__tipo__codigo=CODIGO_PRODUCTO_TERMINADO,
    )
    if p.get("producto"):
        variantes = variantes.filter(producto_id=p["producto"])
    if p.get("categoria"):
        variantes = variantes.filter(producto__categoria_producto_id=p["categoria"])
    if p.get("buscar"):
        b = p["buscar"]
        variantes = variantes.filter(Q(sku__icontains=b) | Q(producto__nombre__icontains=b) | Q(nombre__icontains=b))
    universo = list(variantes.order_by("id").values(
        "id", "sku", "nombre", "producto_id", "producto__nombre", "producto__categoria_producto_id",
        "producto__categoria_producto__nombre", "color__nombre", "talla__nombre",
    ))
    # Las demás consultas filtran por subconsulta, no por una lista de miles de ids.
    ids_variante = variantes.values("id")

    def clave(variante_id, producto_id):
        return ("p", producto_id) if por_producto else ("v", variante_id)

    filas = defaultdict(_fila_vacia)
    info = {}
    for v in universo:
        k = clave(v["id"], v["producto_id"])
        filas[k]
        if k not in info:
            info[k] = {
                "producto_id": v["producto_id"], "producto_nombre": v["producto__nombre"],
                "categoria_id": v["producto__categoria_producto_id"],
                "categoria_nombre": v["producto__categoria_producto__nombre"],
                "variante_id": None if por_producto else v["id"],
                "sku": None if por_producto else v["sku"],
                "color": None if por_producto else v["color__nombre"],
                "talla": None if por_producto else v["talla__nombre"],
                "variantes": 0,
            }
        info[k]["variantes"] += 1

    # 2) Venta: piezas por talla de pedido; clave por la variante de la talla.
    ventas = PedidoDetalleTalla.objects.filter(
        pedido_detalle__pedido__empresa=empresa,
        pedido_detalle__pedido__tipo_pedido=TIPO_PEDIDO_VENTA,
        pedido_detalle__pedido__estatus__in=ESTATUS_PEDIDO_VENDIDO,
        pedido_detalle__pedido__activo=True,
        variante_id__in=ids_variante,
    )
    if ambito is not None:
        ventas = ventas.filter(pedido_detalle__pedido__sucursal_id__in=ambito)
    ventas = ventas.annotate(
        fecha=Coalesce("pedido_detalle__pedido__fecha_confirmacion", "pedido_detalle__pedido__created_at"),
    )
    producto_de = {v["id"]: v["producto_id"] for v in universo}
    en_periodo = Q(fecha__gte=inicio)
    # 2a) Totales e indicadores por variante (toda la historia para la última venta).
    for r in ventas.values("variante_id").annotate(
        vendido=Sum("cantidad", filter=en_periodo),
        anterior=Sum("cantidad", filter=Q(fecha__gte=inicio_anterior, fecha__lt=inicio)),
        ultima=Max("fecha"),
        pedidos=Count("pedido_detalle__pedido", distinct=True, filter=en_periodo),
        clientes=Count("pedido_detalle__pedido__cliente", distinct=True, filter=en_periodo),
    ):
        f = filas[clave(r["variante_id"], producto_de[r["variante_id"]])]
        f["vendido"] += _num(r["vendido"])
        f["anterior"] += _num(r["anterior"])
        f["pedidos"] += r["pedidos"]  # con agrupar=producto es suma por variante (aprox.)
        f["clientes"] += r["clientes"]
        if r["ultima"] and (f["ultima"] is None or r["ultima"] > f["ultima"]):
            f["ultima"] = r["ultima"]
    # 2a-bis) Importe por moneda del pedido: nunca se suman monedas distintas.
    for r in ventas.filter(en_periodo).values("variante_id", "pedido_detalle__pedido__moneda__codigo_iso").annotate(
        importe=Sum(F("cantidad") * Coalesce("precio_unitario", "pedido_detalle__precio_unitario")),
    ):
        f = filas[clave(r["variante_id"], producto_de[r["variante_id"]])]
        iso = r["pedido_detalle__pedido__moneda__codigo_iso"] or "S/M"
        f["importe"][iso] += _num(r["importe"])
    # 2b) Serie semanal del periodo (índice 0 = semana más antigua).
    for r in ventas.filter(en_periodo).annotate(dia=TruncDate("fecha")).values("variante_id", "dia").annotate(
        piezas=Sum("cantidad"),
    ):
        f = filas[clave(r["variante_id"], producto_de[r["variante_id"]])]
        if f["serie"] is None:
            f["serie"] = [0] * semanas
        hace = (hoy - r["dia"]).days // 7
        if 0 <= hace < semanas:
            f["serie"][semanas - 1 - hace] += int(r["piezas"] or 0)

    # 3) Existencia en los almacenes de la empresa (o del ámbito).
    existencias = Existencia.objects.filter(almacen__empresa=empresa, producto_variante_id__in=ids_variante)
    if ambito is not None:
        existencias = existencias.filter(almacen__sucursal_id__in=ambito)
    for r in existencias.values("producto_variante_id").annotate(total=Sum("cantidad")):
        pv = r["producto_variante_id"]
        filas[clave(pv, producto_de[pv])]["existencia"] += _num(r["total"])

    # 4) En proceso: piezas en OPs abiertas.
    en_proceso = OrdenProduccionDetalle.objects.filter(
        op__empresa=empresa, op__activo=True, op__estatus_op__in=ESTATUS_OP_ABIERTA,
        producto_variante_id__in=ids_variante,
    )
    if ambito is not None:
        en_proceso = en_proceso.filter(op__sucursal_id__in=ambito)
    for r in en_proceso.values("producto_variante_id").annotate(total=Sum("cantidad")):
        pv = r["producto_variante_id"]
        filas[clave(pv, producto_de[pv])]["en_proceso"] += _num(r["total"])

    # Derivados.
    filas_out = []
    for k, f in filas.items():
        venta_semanal = f["vendido"] / semanas
        disponible = f["existencia"] + f["en_proceso"]
        cobertura = (disponible / venta_semanal) if venta_semanal > 0 else None
        tendencia = ((f["vendido"] - f["anterior"]) / f["anterior"] * 100) if f["anterior"] > 0 else None
        dias = (hoy - timezone.localtime(f["ultima"]).date()).days if f["ultima"] else None
        resurtido = max(0, math.ceil(venta_semanal * objetivo - disponible)) if venta_semanal > 0 else 0
        filas_out.append({
            **info[k],
            "vendido_periodo": f["vendido"], "vendido_periodo_anterior": f["anterior"],
            "tendencia_pct": round(tendencia, 1) if tendencia is not None else None,
            "venta_semanal": round(venta_semanal, 2),
            "serie_semanal": f["serie"] or [0] * semanas,
            "pedidos": f["pedidos"], "clientes": f["clientes"],
            "ultima_venta": timezone.localtime(f["ultima"]).date() if f["ultima"] else None,
            "dias_sin_venta": dias,
            "existencia": f["existencia"], "en_proceso": f["en_proceso"], "disponible": disponible,
            "cobertura_semanas": round(cobertura, 1) if cobertura is not None else None,
            "resurtido_sugerido": resurtido,
            "importe_por_moneda": {m: v for m, v in sorted(f["importe"].items())},
            # Total solo si todo es de una moneda; con varias, ``null`` (ver ``importe_por_moneda``).
            "importe_vendido": next(iter(f["importe"].values())) if len(f["importe"]) == 1 else (
                CERO if not f["importe"] else None),
        })

    # 5-6) Capacidad de fabricación con la materia prima en existencia (BOM activo).
    boms = capacidad.materiales(empresa, ids_variante, ambito)
    con_bom_por_producto = defaultdict(int)
    for v in universo:
        if v["id"] in boms:
            con_bom_por_producto[v["producto_id"]] += 1
    for r in filas_out:
        if por_producto:
            r.update(capacidad.resumen_variante(None))
            r["tiene_bom"] = con_bom_por_producto[r["producto_id"]] > 0
            r["variantes_con_bom"] = con_bom_por_producto[r["producto_id"]]
            r["puede_fabricar_resurtido"] = None
            continue
        r.update(capacidad.resumen_variante(boms.get(r["variante_id"])))
        r["puede_fabricar_resurtido"] = (
            r["fabricable_con_existencia"] >= r["resurtido_sugerido"]
            if r["resurtido_sugerido"] > 0 and r["fabricable_con_existencia"] is not None else None
        )

    # Clase ABC sobre todo el universo filtrado (antes de clase/estado/paginación).
    total = sum((r["vendido_periodo"] for r in filas_out), CERO)
    acumulado = CERO
    for r in sorted(filas_out, key=lambda r: r["vendido_periodo"], reverse=True):
        if r["vendido_periodo"] <= 0 or total <= 0:
            r["clase_abc"] = "C"
            continue
        previo = acumulado / total
        acumulado += r["vendido_periodo"]
        r["clase_abc"] = "A" if previo < Decimal("0.80") else ("B" if previo < Decimal("0.95") else "C")
        r["participacion_pct"] = round(r["vendido_periodo"] / total * 100, 2)

    umbral = p["dias_sin_venta"]
    for r in filas_out:
        r.setdefault("participacion_pct", Decimal("0"))
        sin_venta = r["dias_sin_venta"] is None or r["dias_sin_venta"] >= umbral
        if sin_venta:
            r["estado"] = SIN_VENTA
        elif r["resurtido_sugerido"] > 0:
            r["estado"] = RESURTIR
        elif r["cobertura_semanas"] is not None and r["cobertura_semanas"] > 2 * objetivo:
            r["estado"] = SOBREINVENTARIO
        elif r["clase_abc"] == "C":
            r["estado"] = LENTO
        else:
            r["estado"] = OK
        # Candidato a descontinuar: sin venta en el umbral, o lento y cayendo.
        r["candidato_descontinuar"] = sin_venta or (
            r["estado"] in (LENTO, SOBREINVENTARIO) and r["tendencia_pct"] is not None and r["tendencia_pct"] < 0
        )

    importe_por_moneda = defaultdict(lambda: CERO)
    for r in filas_out:
        for m, v in r["importe_por_moneda"].items():
            importe_por_moneda[m] += v
    importe_por_moneda = {m: v for m, v in sorted(importe_por_moneda.items())}
    resumen = {
        "articulos": len(filas_out),
        "vendido_periodo": total,
        "vendido_periodo_anterior": sum((r["vendido_periodo_anterior"] for r in filas_out), CERO),
        "existencia": sum((r["existencia"] for r in filas_out), CERO),
        "en_proceso": sum((r["en_proceso"] for r in filas_out), CERO),
        "resurtido_sugerido": sum(r["resurtido_sugerido"] for r in filas_out),
        "importe_por_moneda": importe_por_moneda,
        "por_clase": {c: sum(1 for r in filas_out if r["clase_abc"] == c) for c in "ABC"},
        "por_estado": {e: sum(1 for r in filas_out if r["estado"] == e) for e in ESTADOS},
        "candidatos_descontinuar": sum(1 for r in filas_out if r["candidato_descontinuar"]),
        "con_bom": sum(1 for r in filas_out if r["tiene_bom"]),
        "resurtir_sin_bom": sum(1 for r in filas_out if r["resurtido_sugerido"] > 0 and not r["tiene_bom"]),
        "resurtir_sin_material": sum(1 for r in filas_out if r["puede_fabricar_resurtido"] is False),
    }
    ant = resumen["vendido_periodo_anterior"]
    resumen["tendencia_pct"] = round((total - ant) / ant * 100, 1) if ant > 0 else None

    # Filtros sobre derivados.
    if p.get("clase"):
        filas_out = [r for r in filas_out if r["clase_abc"] == p["clase"]]
    if p.get("estado"):
        filas_out = [r for r in filas_out if r["estado"] == p["estado"]]
    if p.get("solo_candidatos"):
        filas_out = [r for r in filas_out if r["candidato_descontinuar"]]
    fab = p.get("fabricacion")
    if fab == "SIN_BOM":
        filas_out = [r for r in filas_out if not r["tiene_bom"]]
    elif fab == "ALCANZA":
        filas_out = [r for r in filas_out if r["puede_fabricar_resurtido"] is True]
    elif fab == "NO_ALCANZA":
        filas_out = [r for r in filas_out if r["puede_fabricar_resurtido"] is False]

    campo = p["ordering"].lstrip("-")
    desc = p["ordering"].startswith("-")
    campo_real = "producto_nombre" if campo == "nombre" else campo
    con_valor = [r for r in filas_out if r[campo_real] is not None]
    sin_valor = [r for r in filas_out if r[campo_real] is None]
    # Orden total (producto_id, variante_id al final) para que la paginación sea estable.
    def desempate(r):
        return (r["producto_nombre"] or "", r["sku"] or "", r["producto_id"], r["variante_id"] or 0)

    con_valor.sort(key=lambda r: (r[campo_real], *desempate(r)), reverse=desc)
    sin_valor.sort(key=desempate)
    filas_out = con_valor + sin_valor  # los nulos siempre al final

    return {
        "parametros": {
            "semanas": semanas, "semanas_objetivo": objetivo, "dias_sin_venta": umbral,
            "agrupar": p["agrupar"], "desde": timezone.localtime(inicio).date(), "hasta": hoy,
        },
        "resumen": resumen,
        "filas": filas_out,
        "boms": boms,
    }
