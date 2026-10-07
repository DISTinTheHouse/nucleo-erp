"""Trazabilidad del pedido: en qué paso va, cuánto lleva y si va a tiempo.

Todo es DERIVADO de lo que ya registran producción, WMS y finanzas; nada se
captura a mano. Unidad común: piezas (``PedidoDetalleTalla.cantidad``).

``trazabilidad_pedidos()`` calcula varios pedidos con un número fijo de
queries, sin importar cuántas órdenes o renglones tengan. ``trazabilidad_pedido()``
es el mismo cálculo para uno, con sus órdenes de trabajo.

Reglas: ``DOCS/arquitectura/plan-trazabilidad-pedido.md``.
"""

from collections import defaultdict

from django.db.models import Exists, F, FloatField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from finanzas.models import CuentaPorCobrar, Factura, FacturaDetalle
from produccion.models import (
    BordadoAvances,
    OrdenBordadoDetalle,
    OrdenCorteMangaDetalle,
    OrdenesBordado,
    OrdenesCorteManga,
    OrdenesReflejante,
    OrdenProduccion,
    OrdenReflejanteDetalle,
    ReflejanteAvances,
)
from ventas.models import Pedido, PedidoDetalleTalla
from ventas.services.clasificacion_service import fecha_base_compromiso, rango_fecha_entrega
from wms.models import DespachoDetalle, Estado as EstadoWms, PackingDetalle
from wms.services.picking_pipeline.pendientes import asignado_por_pedidos

# --- Semáforo ---------------------------------------------------------------
#: Puntos de avance por debajo del % de plazo consumido.
UMBRAL_AMARILLO = 10
UMBRAL_ROJO = 30
#: % de plazo consumido a partir del cual piezas sin orden de trabajo alertan.
UMBRAL_TIEMPO_SIN_ORDEN = 25

GRIS, TERMINADO, ROJO, AMARILLO, VERDE = "gris", "terminado", "rojo", "amarillo", "verde"

# --- Estados de un paso -----------------------------------------------------
NO_APLICA, PENDIENTE, EN_PROCESO, COMPLETO, DETENIDO = (
    "no_aplica", "pendiente", "en_proceso", "completo", "detenido",
)

PASOS = (
    ("confirmado", "Confirmado"),
    ("programado", "Programado"),
    ("surtido", "Surtido"),
    ("maquila", "Maquila"),
    ("empacado", "Empacado"),
    ("embarcado", "Embarcado"),
)
#: Pasos que cuentan para el avance operativo.
PASOS_AVANCE = ("surtido", "maquila", "empacado", "embarcado")

ESTATUS_PEDIDO_CONFIRMADO = (3, 4)  # AUTORIZADA, EN PROCESO

#: Órdenes de trabajo medidas en piezas. ``avance=None``: el proceso no
#: registra avances (corte de manga) y solo cuenta la orden terminada.
_PROCESOS_OT = (
    {
        "clave": "bordado",
        "label": "Bordado",
        "tipo": "BORDADO",
        "flag": "lleva_bordado",
        "modelo": OrdenesBordado,
        "folio": "folio_bordado",
        "estatus": "estatus_bordado",
        "detalle": OrdenBordadoDetalle,
        "detalle_fk": "ob",
        "avance": BordadoAvances,
        "avance_fk": "ob",
        "avance_cantidad": "cantidad_bordada",
        "inicial": OrdenesBordado.EstatusBordado.SIN_TRABAJAR,
        "terminado": OrdenesBordado.EstatusBordado.FINALIZADO,
        "detenido": OrdenesBordado.EstatusBordado.DETENIDO,
        "cancelados": (OrdenesBordado.EstatusBordado.CANCELADO_LEGACY,),
    },
    {
        "clave": "reflejante",
        "label": "Reflejante",
        "tipo": "REFLEJANTE",
        "flag": "lleva_reflejante",
        "modelo": OrdenesReflejante,
        "folio": "folio_reflejante",
        "estatus": "estatus_reflejante",
        "detalle": OrdenReflejanteDetalle,
        "detalle_fk": "orden_r",
        "avance": ReflejanteAvances,
        "avance_fk": "orden_r",
        "avance_cantidad": "cantidad_aplicada",
        "inicial": OrdenesReflejante.EstatusReflejante.PENDIENTE,
        "terminado": OrdenesReflejante.EstatusReflejante.COMPLETADO,
        "detenido": OrdenesReflejante.EstatusReflejante.DETENIDO,
        "cancelados": (OrdenesReflejante.EstatusReflejante.CANCELADO,),
    },
    {
        "clave": "corte_manga",
        "label": "Corte de manga",
        "tipo": "CORTE_MANGA",
        "flag": "lleva_corte_manga",
        "modelo": OrdenesCorteManga,
        "folio": "folio_ocm",
        "estatus": "estatus_corte",
        "detalle": OrdenCorteMangaDetalle,
        "detalle_fk": "ocm",
        "avance": None,
        "inicial": OrdenesCorteManga.EstatusCorte.PENDIENTE,
        "terminado": OrdenesCorteManga.EstatusCorte.COMPLETADO,
        "detenido": OrdenesCorteManga.EstatusCorte.DETENIDO,
        "cancelados": (OrdenesCorteManga.EstatusCorte.CANCELADO,),
    },
)

_EstatusOP = OrdenProduccion.EstatusOrdenProduccion
#: Hitos de ``OrdenProduccionRutaCritica``; la OP en ``COMPLETADO`` es el último.
_HITOS_OP = (
    ("paquete_tecnico", "ruta_critica__fecha_liberacion_paquete_tecnico"),
    ("kit_completo", "ruta_critica__kit_completo"),
    ("trazo", "ruta_critica__fecha_trazo"),
    ("corte", "ruta_critica__fecha_real_corte"),
    ("corte_recibido", "ruta_critica__corte_recibido"),
)


# --- Helpers ----------------------------------------------------------------

def _num(valor):
    """Piezas para JSON: entero si es exacto, si no 2 decimales."""
    valor = float(valor or 0)
    return int(valor) if valor.is_integer() else round(valor, 2)


def _pct(hecho, total):
    if not total or total <= 0:
        return 0.0
    return round(min(float(hecho) / float(total), 1.0) * 100, 1)


def _promedio(valores):
    valores = list(valores)
    return round(sum(valores) / len(valores), 1) if valores else 0.0


def _estado(pct, iniciado=False, detenido=False):
    if pct >= 100:
        return COMPLETO
    if detenido:
        return DETENIDO
    if pct > 0 or iniciado:
        return EN_PROCESO
    return PENDIENTE


def _paso(clave, label, estado, pct=None, hecho=None, total=None):
    """Forma única de un paso: siempre las mismas llaves."""
    return {
        "clave": clave,
        "label": label,
        "estado": estado,
        "pct": pct,
        "hecho": None if hecho is None else _num(hecho),
        "total": None if total is None else _num(total),
    }


def _paso_piezas(clave, label, hecho, total):
    pct = _pct(hecho, total)
    return _paso(clave, label, _estado(pct), pct, hecho, total)


def _labels_estatus(modelo, campo):
    return dict(modelo._meta.get_field(campo).choices)


# --- Queries (todas agrupadas por pedido) -----------------------------------

def _piezas_por_pedido(pedido_ids):
    """Total de piezas y contratado por proceso, en UNA query.

    Mismo criterio que ``Orden*Service.contratado_por_pedido``: tallas con el
    flag del proceso encendido.
    """
    anotaciones = {"total": Sum("cantidad")}
    for cfg in _PROCESOS_OT:
        anotaciones[cfg["clave"]] = Sum("cantidad", filter=Q(**{cfg["flag"]: True}))
    filas = (
        PedidoDetalleTalla.objects
        .filter(pedido_detalle__pedido_id__in=pedido_ids)
        .values("pedido_detalle__pedido_id")
        .annotate(**anotaciones)
    )
    return {f["pedido_detalle__pedido_id"]: f for f in filas}


def _suma_hijos(modelo, fk, campo, **filtros):
    """``Σ campo`` de los hijos de cada orden, como subquery (sin fan-out de JOIN)."""
    return Coalesce(
        Subquery(
            modelo.objects.filter(**{fk: OuterRef("pk")}, **filtros)
            .values(fk)
            .annotate(t=Sum(campo))
            .values("t")[:1],
            output_field=FloatField(),
        ),
        Value(0.0),
        output_field=FloatField(),
    )


def _ordenes_trabajo(cfg, pedido_ids):
    """Órdenes vigentes del proceso con piezas cubiertas y avanzadas, en UNA query."""
    if cfg["avance"] is not None:
        avanzado = _suma_hijos(cfg["avance"], cfg["avance_fk"], cfg["avance_cantidad"], activo=True)
    else:
        avanzado = Value(0.0, output_field=FloatField())
    return (
        cfg["modelo"].objects
        .filter(pedido_id__in=pedido_ids, activo=True)
        .exclude(**{f"{cfg['estatus']}__in": cfg["cancelados"]})
        .annotate(
            cubierto=_suma_hijos(cfg["detalle"], cfg["detalle_fk"], "cantidad"),
            avanzado=avanzado,
        )
        .order_by("id")
        .values(
            "id", "pedido_id", "cubierto", "avanzado",
            folio=F(cfg["folio"]), estatus=F(cfg["estatus"]),
        )
    )


def _ordenes_produccion(pedido_ids):
    return (
        OrdenProduccion.objects
        .filter(pedido_id__in=pedido_ids, activo=True)
        .exclude(estatus_op=_EstatusOP.CANCELADO)
        .order_by("op_id")
        .values("op_id", "pedido_id", "folio_op", "estatus_op", *[campo for _, campo in _HITOS_OP])
    )


def _suma_por_pedido(qs, campo_pedido, campo):
    filas = qs.values(campo_pedido).annotate(t=Sum(campo))
    return {f[campo_pedido]: float(f["t"] or 0) for f in filas}


def _empacado_y_embarcado(pedido_ids):
    base = (
        PackingDetalle.objects
        .filter(packing__pedido_id__in=pedido_ids)
        .exclude(packing__estado=EstadoWms.CANCELADO)
        .exclude(estado=EstadoWms.CANCELADO)
    )
    empacado = _suma_por_pedido(base, "packing__pedido_id", "cantidad_empacada")
    embarcado = _suma_por_pedido(
        base.filter(Exists(DespachoDetalle.objects.filter(packing_detalle=OuterRef("pk")))),
        "packing__pedido_id",
        "cantidad_empacada",
    )
    return empacado, embarcado


def _facturado(pedido_ids):
    qs = FacturaDetalle.objects.filter(
        factura__pedido_id__in=pedido_ids,
        factura__activo=True,
        factura__estatus=Factura.FacturaStatus.EMITIDA,
    )
    return _suma_por_pedido(qs, "factura__pedido_id", "cantidad")


def _cobrado_pct(pedido_ids):
    """``{pedido_id: % cobrado}`` sobre sus cuentas por cobrar vigentes."""
    filas = (
        CuentaPorCobrar.objects
        .filter(factura__pedido_id__in=pedido_ids, factura__activo=True)
        .exclude(estatus=CuentaPorCobrar.EstatusCxC.CANCELADA)
        .exclude(factura__estatus=Factura.FacturaStatus.CANCELADA)
        .values("factura__pedido_id")
        .annotate(total=Sum("total"), saldo=Sum("saldo"))
    )
    return {
        f["factura__pedido_id"]: _pct((f["total"] or 0) - (f["saldo"] or 0), f["total"])
        for f in filas
        if f["total"]
    }


# --- Armado -----------------------------------------------------------------

def _proceso_ot(cfg, contratado, filas, labels):
    """Paso de maquila de un proceso por piezas, con sus órdenes."""
    ordenes, cubierto, hecho = [], 0.0, 0.0
    iniciado = detenido = False
    todas_terminadas = bool(filas)
    for fila in filas:
        terminada = fila["estatus"] == cfg["terminado"]
        orden_hecho = fila["cubierto"] if terminada else min(fila["avanzado"], fila["cubierto"])
        cubierto += fila["cubierto"]
        hecho += orden_hecho
        iniciado = iniciado or fila["estatus"] != cfg["inicial"]
        detenido = detenido or fila["estatus"] == cfg["detenido"]
        todas_terminadas = todas_terminadas and terminada
        ordenes.append({
            "tipo": cfg["tipo"],
            "id": fila["id"],
            "folio": fila["folio"],
            "estatus_label": labels.get(fila["estatus"], str(fila["estatus"])),
            "pct": 100.0 if terminada else _pct(orden_hecho, fila["cubierto"]),
            "hecho": _num(orden_hecho),
            "cubierto": _num(fila["cubierto"]),
            "detenida": fila["estatus"] == cfg["detenido"],
        })

    total = contratado if contratado > 0 else cubierto
    if total <= 0 and not ordenes:
        proceso = _paso(cfg["clave"], cfg["label"], NO_APLICA)
    else:
        if total > 0:
            hecho = min(hecho, total)
            pct = _pct(hecho, total)
        else:
            pct = 100.0 if todas_terminadas else 0.0
        proceso = _paso(
            cfg["clave"], cfg["label"], _estado(pct, iniciado, detenido), pct, hecho, total
        )
    proceso["sin_orden"] = _num(max(0.0, contratado - cubierto))
    proceso["ordenes"] = ordenes
    return proceso


def _proceso_op(filas, labels):
    """Paso de maquila de las OP: avance por hitos de ruta crítica."""
    ordenes = []
    for fila in filas:
        completada = fila["estatus_op"] == _EstatusOP.COMPLETADO
        hitos = sum(bool(fila[campo]) for _, campo in _HITOS_OP)
        ordenes.append({
            "tipo": "OP",
            "id": fila["op_id"],
            "folio": fila["folio_op"],
            "estatus_label": labels.get(fila["estatus_op"], str(fila["estatus_op"])),
            "pct": 100.0 if completada else _pct(hitos, len(_HITOS_OP) + 1),
            "hecho": None,
            "cubierto": None,
            "detenida": fila["estatus_op"] == _EstatusOP.DETENIDO,
        })
    if not ordenes:
        proceso = _paso("op", "Orden de producción", NO_APLICA)
    else:
        pct = _promedio(o["pct"] for o in ordenes)
        estado = _estado(
            pct,
            iniciado=any(f["estatus_op"] != _EstatusOP.PENDIENTE for f in filas),
            detenido=any(o["detenida"] for o in ordenes),
        )
        proceso = _paso("op", "Orden de producción", estado, pct)
    proceso["sin_orden"] = 0
    proceso["ordenes"] = ordenes
    return proceso


def _paso_maquila(procesos):
    aplicables = [p for p in procesos if p["estado"] != NO_APLICA]
    if not aplicables:
        paso = _paso("maquila", "Maquila", NO_APLICA)
    else:
        pct = _promedio(p["pct"] for p in aplicables)
        estado = _estado(
            pct,
            iniciado=any(p["estado"] != PENDIENTE for p in aplicables),
            detenido=any(p["estado"] == DETENIDO for p in aplicables),
        )
        paso = _paso("maquila", "Maquila", estado, pct)
    paso["procesos"] = procesos
    return paso


def _paso_confirmado(pedido):
    if pedido.estatus not in ESTATUS_PEDIDO_CONFIRMADO:
        return _paso("confirmado", "Confirmado", PENDIENTE, 0.0)
    if pedido.clasificacion and pedido.fecha_confirmacion:
        return _paso("confirmado", "Confirmado", COMPLETO, 100.0)
    # Autorizado, pero mesa de control no lo ha clasificado/confirmado.
    return _paso("confirmado", "Confirmado", EN_PROCESO, 50.0)


def _programado(pedido):
    programaciones = (pedido.programacion_conf or {}).get("programaciones") or []
    total = 0
    for p in programaciones:
        try:
            total += int(p.get("cantidad") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
    return total


def _paso_actual(pasos):
    """Último paso aplicable que ya arrancó; si ninguno, el primero aplicable.

    No es "el primer paso incompleto": la programación es parcial u opcional y
    con esa regla un pedido ya empacado se quedaría "en Programado".
    """
    aplicables = [p for p in pasos if p["estado"] != NO_APLICA]
    arrancados = [p for p in aplicables if p["estado"] != PENDIENTE]
    return (arrancados or aplicables)[-1]["clave"]


def _semaforo(pedido, avance, procesos, embarcado_completo, hoy):
    """``(color, motivos)``."""
    if pedido.estatus == Pedido.ESTATUS_CANCELADO:
        return GRIS, ["Pedido cancelado"]
    if not pedido.clasificacion:
        return GRIS, ["Sin clasificación: no hay fecha compromiso"]
    if pedido.clasificacion == Pedido.Clasificacion.X:
        return GRIS, ["Clasificación X: solo para facturar"]
    if embarcado_completo:
        return TERMINADO, []

    inicio = fecha_base_compromiso(pedido)
    _, compromiso = rango_fecha_entrega(pedido)
    if inicio is None or compromiso is None:
        return GRIS, ["Sin fecha compromiso"]

    plazo = max((compromiso - inicio).days, 1)
    tiempo_pct = max((hoy - inicio).days, 0) / plazo * 100
    color, motivos = VERDE, []

    if hoy > compromiso:
        color = ROJO
        motivos.append(f"Vencido desde {compromiso:%d/%m/%Y} ({(hoy - compromiso).days} días)")
    elif avance < tiempo_pct - UMBRAL_AMARILLO:
        color = ROJO if avance < tiempo_pct - UMBRAL_ROJO else AMARILLO
        motivos.append(f"Avance {avance:.0f}% con {tiempo_pct:.0f}% del plazo consumido")

    for proceso in procesos:
        for orden in proceso["ordenes"]:
            if orden["detenida"]:
                color = ROJO if color == ROJO else AMARILLO
                motivos.append(f"{orden['folio']} detenida")
        if proceso["sin_orden"] > 0 and tiempo_pct > UMBRAL_TIEMPO_SIN_ORDEN:
            color = ROJO if color == ROJO else AMARILLO
            motivos.append(f"{proceso['sin_orden']} pzs de {proceso['label'].lower()} sin orden de trabajo")

    return color, motivos


def trazabilidad_pedidos(pedidos, incluir_ordenes=False):
    """``{pedido_id: payload}`` para varios pedidos, con queries constantes.

    ``incluir_ordenes=False`` (tablero) deja ``ordenes`` vacío en cada
    proceso; el cálculo es el mismo.
    """
    pedidos = list(pedidos)
    if not pedidos:
        return {}
    ids = [p.pk for p in pedidos]
    hoy = timezone.localdate()

    piezas = _piezas_por_pedido(ids)
    surtido = asignado_por_pedidos(ids)
    empacado, embarcado = _empacado_y_embarcado(ids)
    facturado = _facturado(ids)
    cobrado = _cobrado_pct(ids)

    ordenes_ot = {}
    for cfg in _PROCESOS_OT:
        agrupadas = defaultdict(list)
        for fila in _ordenes_trabajo(cfg, ids):
            agrupadas[fila["pedido_id"]].append(fila)
        ordenes_ot[cfg["clave"]] = (agrupadas, _labels_estatus(cfg["modelo"], cfg["estatus"]))
    ops = defaultdict(list)
    for fila in _ordenes_produccion(ids):
        ops[fila["pedido_id"]].append(fila)
    labels_op = _labels_estatus(OrdenProduccion, "estatus_op")

    estatus_labels = dict(Pedido.CHOICES_ESTATUS)
    resultado = {}
    for pedido in pedidos:
        fila_piezas = piezas.get(pedido.pk) or {}
        total = float(fila_piezas.get("total") or 0)

        procesos = []
        for cfg in _PROCESOS_OT:
            agrupadas, labels = ordenes_ot[cfg["clave"]]
            contratado = float(fila_piezas.get(cfg["clave"]) or 0)
            procesos.append(_proceso_ot(cfg, contratado, agrupadas.get(pedido.pk, []), labels))
        procesos.append(_proceso_op(ops.get(pedido.pk, []), labels_op))

        pasos = [
            _paso_confirmado(pedido),
            _paso_piezas("programado", "Programado", _programado(pedido), total),
            _paso_piezas("surtido", "Surtido", surtido.get(pedido.pk, 0), total),
            _paso_maquila(procesos),
            _paso_piezas("empacado", "Empacado", empacado.get(pedido.pk, 0), total),
            _paso_piezas("embarcado", "Embarcado", embarcado.get(pedido.pk, 0), total),
        ]
        avance = _promedio(
            p["pct"] for p in pasos if p["clave"] in PASOS_AVANCE and p["estado"] != NO_APLICA
        )
        color, motivos = _semaforo(pedido, avance, procesos, pasos[-1]["estado"] == COMPLETO, hoy)

        if not incluir_ordenes:
            for proceso in procesos:
                proceso["ordenes"] = []

        _, compromiso = rango_fecha_entrega(pedido)
        resultado[pedido.pk] = {
            "pedido": {
                "id": pedido.pk,
                "folio": pedido.folio,
                "cliente": pedido.cliente_nombre or pedido.cliente_razon_social,
                "estatus_label": estatus_labels.get(pedido.estatus, str(pedido.estatus)),
                "clasificacion": pedido.clasificacion,
                "fecha_compromiso": compromiso,
                "dias_restantes": (compromiso - hoy).days if compromiso else None,
                "total_piezas": _num(total),
            },
            "resumen": {
                "paso_actual": _paso_actual(pasos),
                "avance": avance,
                "semaforo": color,
                "motivos": motivos,
                "facturado_pct": _pct(facturado.get(pedido.pk, 0), total),
                "cobrado_pct": cobrado.get(pedido.pk),
            },
            "pasos": pasos,
        }
    return resultado


def trazabilidad_pedido(pedido):
    """Trazabilidad de un pedido, con sus órdenes de trabajo."""
    return trazabilidad_pedidos([pedido], incluir_ordenes=True)[pedido.pk]


