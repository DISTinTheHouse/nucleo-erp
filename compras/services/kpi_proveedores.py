"""Scorecard por proveedor (EC-436): puntualidad, calidad, cumplimiento de
cantidad y diferencia de precio, sobre datos reales de compras.

Los criterios de cumplimiento, rechazo y precio son los de
``compras.services.kpi_criterios`` (los mismos del KPI de recepciones, EC-434).

- **Puntualidad**: recepción de OC con fecha prometida; a tiempo si su fecha
  LOCAL (America/Mexico_City) es <= la fecha prometida (#388).
- **Lead time**: real (fecha de recepción - fecha de la OC) y pactado (fecha
  prometida - fecha de la OC) se promedian sobre las MISMAS recepciones, las que
  tienen ambas fechas, así que son comparables (#388).
- **Scorecard**: promedio simple de los factores que sí tienen dato; si falta
  uno se re-normaliza entre los demás.
- Proveedores inactivos no entran (``AlcanceCompras.solo_proveedores_activos``).
"""

from __future__ import annotations

from decimal import Decimal

from django.db.models import F, Q, Sum
from django.utils import timezone

from compras.models import Recepcion
from compras.services.kpi_criterios import (
    AlcanceCompras,
    con_importes_precio,
    cumplimiento_pendiente_por_oc,
    cumplimiento_por_unidad,
    inspecciones_oc,
    lineas_factura_proveedor,
    misma_moneda,
    porcentaje,
    rechazos_oc,
    RESULTADO_RECHAZO,
)

META_SCORECARD = 80.0
LIMITE_PROVEEDORES = 50
TOPE_LIMITE_PROVEEDORES = 200
TOPE_DETALLE = 100


def semaforo(valor, meta):
    if valor is None:
        return "sin_datos"
    if valor >= meta:
        return "verde"
    if valor >= meta - 10:
        return "amarillo"
    return "rojo"


def _fecha_local(valor):
    return timezone.localtime(valor).date() if valor else None


def _recepciones_oc(alcance: AlcanceCompras):
    return Recepcion.objects.filter(
        alcance.oc("orden_compra__"),
        activo=True,
        tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA,
    ).exclude(estatus=Recepcion.EstatusRecepcion.CANCELADA)


def calcular_scorecards(alcance: AlcanceCompras, meta: float = META_SCORECARD):
    """Lista completa de proveedores con su scorecard (sin ordenar ni acotar)."""
    por_proveedor: dict = {}

    def bucket(pid, nombre):
        return por_proveedor.setdefault(pid, {
            "proveedor_id": pid, "proveedor_nombre": nombre,
            "recepciones_total": 0, "recepciones_con_compromiso": 0, "recepciones_a_tiempo": 0,
            "recepciones_con_lead_time": 0, "dias_reales": 0, "dias_pactados": 0,
            "cantidad_rechazada": Decimal("0"), "cantidad_inspeccionada": Decimal("0"),
            "unidades": {}, "monedas": {},
        })

    # 1) Puntualidad + lead time: una fila por recepción contra su OC.
    for row in _recepciones_oc(alcance).values(
        "orden_compra__proveedor_id", "orden_compra__proveedor__nombre", "fecha_recepcion",
        "orden_compra__fecha_oc", "orden_compra__fecha_entrega_estimada",
    ):
        b = bucket(row["orden_compra__proveedor_id"], row["orden_compra__proveedor__nombre"])
        b["recepciones_total"] += 1
        fecha_recepcion = _fecha_local(row["fecha_recepcion"])
        fecha_compromiso = row["orden_compra__fecha_entrega_estimada"]
        fecha_oc = row["orden_compra__fecha_oc"]
        # Sin fecha prometida no se puede saber si llegó tarde: no cuenta (#387).
        if fecha_compromiso and fecha_recepcion:
            b["recepciones_con_compromiso"] += 1
            if fecha_recepcion <= fecha_compromiso:
                b["recepciones_a_tiempo"] += 1
            if fecha_oc:
                b["recepciones_con_lead_time"] += 1
                b["dias_reales"] += (fecha_recepcion - fecha_oc).days
                b["dias_pactados"] += (fecha_compromiso - fecha_oc).days

    # 2) Calidad: rechazado (resultado = rechazo) vs. inspeccionado.
    for row in (
        inspecciones_oc(alcance)
        .values(
            "recepcion_detalle__orden_compra_detalle__orden_compra__proveedor_id",
            "recepcion_detalle__orden_compra_detalle__orden_compra__proveedor__nombre",
        )
        .annotate(
            rechazada=Sum("cantidad_rechazada", filter=Q(resultado=RESULTADO_RECHAZO)),
            inspeccionada=Sum("cantidad_inspeccionada"),
        )
        .order_by()
    ):
        b = bucket(
            row["recepcion_detalle__orden_compra_detalle__orden_compra__proveedor_id"],
            row["recepcion_detalle__orden_compra_detalle__orden_compra__proveedor__nombre"],
        )
        b["cantidad_rechazada"] += row["rechazada"] or 0
        b["cantidad_inspeccionada"] += row["inspeccionada"] or 0

    # 3) Cumplimiento de cantidad: ordenado vs. recibido neto, por unidad.
    for (pid, uid), fila in cumplimiento_por_unidad(alcance, por_proveedor=True).items():
        b = bucket(pid, fila["proveedor_nombre"])
        b["unidades"][uid] = fila

    # 4) Diferencia de precio: facturado vs. pactado, por moneda.
    moneda = "factura_proveedor__moneda__codigo_iso"
    for row in (
        con_importes_precio(lineas_factura_proveedor(alcance))
        .filter(misma_moneda())
        .values("oc_detalle__orden_compra__proveedor_id", "oc_detalle__orden_compra__proveedor__nombre", moneda)
        .annotate(facturado=Sum("facturado_linea"), pactado=Sum("pactado_linea"))
        .order_by()
    ):
        b = bucket(row["oc_detalle__orden_compra__proveedor_id"], row["oc_detalle__orden_compra__proveedor__nombre"])
        b["monedas"][row[moneda]] = {
            "facturado": row["facturado"] or Decimal("0"), "pactado": row["pactado"] or Decimal("0"),
        }

    proveedores = []
    for b in por_proveedor.values():
        pct_a_tiempo = porcentaje(b["recepciones_a_tiempo"], b["recepciones_con_compromiso"])
        n_lt = b["recepciones_con_lead_time"]
        dias_real = round(b["dias_reales"] / n_lt, 1) if n_lt else None
        dias_pactado = round(b["dias_pactados"] / n_lt, 1) if n_lt else None
        pct_rechazo = porcentaje(b["cantidad_rechazada"], b["cantidad_inspeccionada"])

        unidades = sorted(b["unidades"].values(), key=lambda f: (f["unidad"] is None, f["unidad"] or ""))
        for u in unidades:
            u["pct"] = porcentaje(u["cantidad_recibida"], u["cantidad_ordenada"])
        mezcla_unidades = len(unidades) > 1
        ordenado = sum((u["cantidad_ordenada"] for u in unidades), Decimal("0"))
        recibido = sum((u["cantidad_recibida"] for u in unidades), Decimal("0"))
        # Cantidades de unidades distintas no se suman: sin pct si hay varias.
        pct_cumplimiento = None if mezcla_unidades else porcentaje(recibido, ordenado)

        por_moneda = [
            {
                "moneda": cod, "costo_facturado": m["facturado"], "costo_pactado_oc": m["pactado"],
                "pct": porcentaje(m["facturado"] - m["pactado"], m["pactado"]),
            }
            for cod, m in sorted(b["monedas"].items())
        ]
        pct_precio = por_moneda[0]["pct"] if len(por_moneda) == 1 else None

        factores = []
        if pct_a_tiempo is not None:
            factores.append(pct_a_tiempo)
        if pct_rechazo is not None:
            factores.append(max(0.0, 100 - pct_rechazo))
        if pct_cumplimiento is not None:
            factores.append(min(max(pct_cumplimiento, 0.0), 100.0))
        if pct_precio is not None:
            factores.append(max(0.0, 100 - abs(pct_precio)))
        scorecard = round(sum(factores) / len(factores), 1) if factores else None

        proveedores.append({
            "proveedor_id": b["proveedor_id"],
            "proveedor_nombre": b["proveedor_nombre"],
            "entrega_a_tiempo": {
                "pct": pct_a_tiempo,
                "recepciones_a_tiempo": b["recepciones_a_tiempo"],
                "recepciones_total": b["recepciones_total"],
                "recepciones_con_compromiso": b["recepciones_con_compromiso"],
            },
            "calidad": {
                "pct_rechazado": pct_rechazo,
                "cantidad_rechazada": b["cantidad_rechazada"],
                "cantidad_inspeccionada": b["cantidad_inspeccionada"],
            },
            "lead_time": {
                "dias_promedio_real": dias_real,
                "dias_promedio_pactado": dias_pactado,
                # Recepciones sobre las que se promedian AMBOS (con las dos fechas).
                "recepciones_medidas": n_lt,
            },
            "cumplimiento_cantidad": {
                "pct": pct_cumplimiento,
                "mezcla_unidades": mezcla_unidades,
                "cantidad_ordenada": None if mezcla_unidades else ordenado,
                "cantidad_recibida": None if mezcla_unidades else recibido,
                "por_unidad": [
                    {k: u[k] for k in ("unidad_id", "unidad", "cantidad_ordenada", "cantidad_recibida", "pct")}
                    for u in unidades
                ],
            },
            "diferencia_precio": {"pct": pct_precio, "por_moneda": por_moneda},
            "scorecard": {"puntaje": scorecard, "semaforo": semaforo(scorecard, meta)},
        })

    # Orden estable: puntaje desc (sin dato al final), luego nombre e id.
    proveedores.sort(key=lambda r: (
        r["scorecard"]["puntaje"] is None,
        -(r["scorecard"]["puntaje"] or 0),
        (r["proveedor_nombre"] or "").lower(),
        r["proveedor_id"],
    ))
    return proveedores


# --- drill-down de un proveedor ------------------------------------------------

FACTORES_DETALLE = ("puntualidad", "calidad", "cumplimiento", "precio")


def detalle_proveedor(alcance: AlcanceCompras, factor: str):
    """Registros detrás de un factor del scorecard de UN proveedor (el
    ``proveedor_id`` va en ``alcance``). Devuelve ``(filas, total)`` con las
    filas acotadas a ``TOPE_DETALLE``."""
    if factor == "puntualidad":
        qs = _recepciones_oc(alcance).order_by("-fecha_recepcion", "-id")
        total = qs.count()
        filas = []
        for r in qs.values(
            "id", "folio", "orden_compra_id", "orden_compra__folio", "fecha_recepcion",
            "orden_compra__fecha_entrega_estimada",
        )[:TOPE_DETALLE]:
            fecha = _fecha_local(r["fecha_recepcion"])
            compromiso = r["orden_compra__fecha_entrega_estimada"]
            filas.append({
                "recepcion_id": r["id"], "recepcion_folio": r["folio"],
                "oc_id": r["orden_compra_id"], "oc_folio": r["orden_compra__folio"],
                "fecha_recepcion": fecha, "fecha_entrega_estimada": compromiso,
                "a_tiempo": (fecha <= compromiso) if (fecha and compromiso) else None,
            })
        return filas, total
    if factor == "calidad":
        qs = rechazos_oc(alcance).order_by("-cantidad_rechazada", "-id")
        total = qs.count()
        filas = [
            {
                "id": r["id"], "recepcion_id": r["recepcion_detalle__recepcion_id"],
                "recepcion_folio": r["recepcion_detalle__recepcion__folio"],
                "producto": r["recepcion_detalle__producto__nombre"],
                "cantidad_rechazada": r["cantidad_rechazada"], "motivo_rechazo": r["motivo_rechazo"],
            }
            for r in qs.values(
                "id", "recepcion_detalle__recepcion_id", "recepcion_detalle__recepcion__folio",
                "recepcion_detalle__producto__nombre", "cantidad_rechazada", "motivo_rechazo",
            )[:TOPE_DETALLE]
        ]
        return filas, total
    if factor == "cumplimiento":
        pendientes = cumplimiento_pendiente_por_oc(alcance)
        return pendientes[:TOPE_DETALLE], len(pendientes)
    if factor == "precio":
        qs = (
            con_importes_precio(lineas_factura_proveedor(alcance))
            .filter(misma_moneda())
            .exclude(facturado_linea=F("pactado_linea"))
            .order_by("-id")
        )
        total = qs.count()
        filas = [
            {
                "id": r["id"], "factura_id": r["factura_proveedor_id"], "factura_folio": r["factura_proveedor__folio"],
                "oc_id": r["oc_detalle__orden_compra_id"], "producto": r["oc_detalle__producto__nombre"],
                "moneda": r["factura_proveedor__moneda__codigo_iso"],
                "precio_facturado": r["precio_unitario"], "precio_pactado": r["oc_detalle__precio"],
            }
            for r in qs.values(
                "id", "factura_proveedor_id", "factura_proveedor__folio", "oc_detalle__orden_compra_id",
                "oc_detalle__producto__nombre", "factura_proveedor__moneda__codigo_iso", "precio_unitario",
                "oc_detalle__precio",
            )[:TOPE_DETALLE]
        ]
        return filas, total
    raise ValueError(factor)
