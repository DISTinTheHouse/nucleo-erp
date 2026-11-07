"""Listado de cotizaciones en modo servidor (EC-811): filtros compartidos por el
listado y sus acciones agregadas.

Lo consume Ventas (``/sales/quotes`` y el dashboard de ``/sales``), que hoy baja la
lista completa y filtra, cuenta y arma el tablero en el navegador.

Los filtros van SIEMPRE encima del alcance (``ventas.scope``, vía
``CotizacionViewSet.get_queryset``): sólo pueden acotar lo que el usuario ya ve.
Un id ajeno (cliente o vendedor de otra empresa, o de otro vendedor) no trae
filas; nunca responde 400, para no servir de oráculo de existencia.

Mismo patrón que ``pedidos_listado`` (EC-782): los filtros se aplican en todas las
``ACCIONES_FILTRADAS``; las anotaciones y el ``only()`` del listado, sólo donde se
serializan filas.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Count, F, Q, Sum, Value, Window
from django.db.models.functions import Coalesce, JSONObject, NullIf, RowNumber, Trim
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from nucleo.kpis import entero_param, rango_fechas
from nucleo.listados import estatus_param
from nucleo.opciones_filtros import clave_orden
from ventas.models import Cotizacion

# Acciones que pasan por los filtros del listado en ``CotizacionViewSet._apply_filters``.
# Las de detalle no: un filtro suelto en una acción de detalle (``get_object()``
# también pasa por ``get_queryset``) puede dar 404 sobre una cotización visible.
ACCION_KPIS = "kpis"
ACCION_TABLERO = "tablero"
ACCIONES_FILTRADAS = ("list", ACCION_KPIS, ACCION_TABLERO)

# Orden por defecto del listado (EC-619): más reciente primero, ``-id`` de desempate.
# ``nulls_last``: ``created_at`` es NULL-able (filas anteriores a la migración que lo
# agregó); en PostgreSQL un DESC las pondría al principio. El tablero ordena cada
# columna con ESTE mismo orden, para que "ver más" (``?estatus=N&offset=k``) continúe
# justo donde terminó la columna.
ORDEN_POR_DEFECTO = (F("created_at").desc(nulls_last=True), F("id").desc())

POR_COLUMNA_DEFECTO = 20
POR_COLUMNA_MAXIMO = 100

ESTATUS_VALIDOS = frozenset(e for e, _ in Cotizacion.CHOICES_ESTATUS)
SIN_CLIENTE = "sin_cliente"
ETIQUETA_SIN_CLIENTE = "Sin cliente"
CENTAVOS = Decimal("0.01")
# Parámetros de ``filtrar`` (los que aceptan ``kpis/`` y ``tablero/``).
PARAMETROS_FILTRO = frozenset({"q", "estatus", "cliente", "vendedor", "fecha_desde", "fecha_hasta"})


def cliente_param(request):
    """``?cliente=<id>`` → entero; ``?cliente=sin_cliente`` → ``SIN_CLIENTE``;
    vacío → ``None``; cualquier otro valor → 400."""
    valor = (request.query_params.get("cliente") or "").strip()
    if not valor:
        return None
    if valor == SIN_CLIENTE:
        return SIN_CLIENTE
    try:
        return int(valor)
    except ValueError:
        raise ValidationError({"cliente": f"Debe ser un entero válido o `{SIN_CLIENTE}`."})


def inicio_del_dia(fecha):
    """Medianoche local (``TIME_ZONE``) de ``fecha`` como datetime aware."""
    return timezone.make_aware(datetime.combine(fecha, time.min))


def filtrar(request, qs):
    """Filtros del listado sobre columnas de ``cotizaciones`` (y la búsqueda ``q``).
    Inválidos → 400.

    - ``estatus``: uno o varios del catálogo (``estatus_param``).
    - ``cliente``: id o ``sin_cliente``.
    - ``vendedor``: id.
    - ``fecha_desde``/``fecha_hasta``: día local de ``created_at``, ambos inclusivos.
      Como rango aware (``>=`` medianoche de ``desde``, ``<`` medianoche del día
      siguiente a ``hasta``) y no ``created_at__date``: el cast a fecha local
      descarta el índice de ``created_at``.
    - ``q``: OC, razón social, nombre o RFC del cliente; si es numérico, también el id.
    """
    estatus = estatus_param(request, ESTATUS_VALIDOS)
    if estatus is not None:
        qs = qs.filter(estatus__in=estatus)

    cliente = cliente_param(request)
    if cliente == SIN_CLIENTE:
        qs = qs.filter(cliente__isnull=True)
    elif cliente is not None:
        qs = qs.filter(cliente_id=cliente)

    vendedor = entero_param(request, "vendedor")
    if vendedor is not None:
        qs = qs.filter(vendedor_id=vendedor)

    fecha_desde, fecha_hasta = rango_fechas(request)
    if fecha_desde:
        qs = qs.filter(created_at__gte=inicio_del_dia(fecha_desde))
    if fecha_hasta:
        qs = qs.filter(created_at__lt=inicio_del_dia(fecha_hasta + timedelta(days=1)))

    q = (request.query_params.get("q") or "").strip()
    if q:
        q_filter = (
            Q(oc__icontains=q)
            | Q(cliente__razon_social__icontains=q)
            | Q(cliente__nombre__icontains=q)
            | Q(cliente__rfc__icontains=q)
        )
        if q.isdigit():
            q_filter = q_filter | Q(id=int(q))
        qs = qs.filter(q_filter)
    return qs


# --- KPIs --------------------------------------------------------------------------


def _importe(valor):
    """Texto con dos decimales, como ``gran_total`` en el listado."""
    return str((valor or Decimal("0")).quantize(CENTAVOS))


def kpis(qs):
    """Conteos por estatus y ``gran_total`` por moneda del conjunto filtrado, en UNA
    consulta agrupada por ``(estatus, moneda)``. ``por_estatus`` trae siempre los 5
    estatus del catálogo (en cero si no hay); la moneda nula es un grupo más
    (``moneda_id``/``moneda`` en ``null``) y va al final. ``order_by()`` antes de
    agrupar: el orden por defecto del listado entraría al ``GROUP BY``."""
    filas = list(
        qs.order_by()
        .values("estatus", "moneda_id", "moneda__codigo_iso")
        .annotate(total=Count("id"), gran_total=Sum("gran_total"))
        .order_by("moneda__codigo_iso", "moneda_id", "estatus")
    )
    por_moneda = {}
    por_estatus = {e: {"total": 0, "por_moneda": []} for e in ESTATUS_VALIDOS}
    for f in filas:
        moneda = {"moneda_id": f["moneda_id"], "moneda": f["moneda__codigo_iso"]}
        acumulado = por_moneda.setdefault(f["moneda_id"], {**moneda, "total": 0, "gran_total": Decimal("0")})
        acumulado["total"] += f["total"]
        acumulado["gran_total"] += f["gran_total"] or Decimal("0")
        bloque = por_estatus[f["estatus"]]
        bloque["total"] += f["total"]
        bloque["por_moneda"].append({**moneda, "total": f["total"], "gran_total": _importe(f["gran_total"])})
    etiquetas = dict(Cotizacion.CHOICES_ESTATUS)
    return {
        "total": sum(f["total"] for f in filas),
        "por_estatus": [
            {"estatus": e, "estatus_label": etiquetas[e], **por_estatus[e]}
            for e, _ in Cotizacion.CHOICES_ESTATUS
        ],
        "por_moneda": [{**m, "gran_total": _importe(m["gran_total"])} for m in por_moneda.values()],
    }


# --- Tablero -----------------------------------------------------------------------


def por_columna_param(request):
    """``?por_columna=`` entero de 1 a ``POR_COLUMNA_MAXIMO``; vacío → el default."""
    valor = entero_param(request, "por_columna")
    if valor is None:
        return POR_COLUMNA_DEFECTO
    if not 1 <= valor <= POR_COLUMNA_MAXIMO:
        raise ValidationError({"por_columna": f"Debe ser un entero entre 1 y {POR_COLUMNA_MAXIMO}."})
    return valor


def ventana_tablero(qs, por_columna):
    """``[(id, estatus, total_del_estatus)]`` de las primeras ``por_columna``
    cotizaciones de cada estatus, en el orden del listado, en UNA consulta.

    Ventana ligera a propósito: sólo ids y conteos sobre el queryset filtrado SIN las
    anotaciones del listado. Con ellas (subconsultas de ``piezas``/``pedido_*``),
    Postgres las calcula para todas las filas antes de recortar por ``fila``: ~950 ms
    con 50 000 cotizaciones contra ~75 ms así (EC-811). ``total_del_estatus`` es el
    ``count`` que daría el listado con ``?estatus=N`` y los mismos filtros. Un
    estatus sin filas no aparece."""
    return list(
        qs.annotate(
            fila_tablero=Window(RowNumber(), partition_by=[F("estatus")], order_by=ORDEN_POR_DEFECTO),
            total_estatus=Window(Count("id"), partition_by=[F("estatus")]),
        )
        .filter(fila_tablero__lte=por_columna)
        .order_by("estatus", "fila_tablero")
        .values_list("id", "estatus", "total_estatus")
    )


def columnas_tablero(ventana, filas_serializadas):
    """Las 5 columnas del catálogo con su ``total`` y sus filas, en el orden de la
    ventana. ``filas_serializadas``: ``{id: fila}``."""
    totales, results = {}, {e: [] for e in ESTATUS_VALIDOS}
    for pk, estatus, total in ventana:
        totales[estatus] = total
        if pk in filas_serializadas:
            results[estatus].append(filas_serializadas[pk])
    return [
        {"estatus": e, "estatus_label": etiqueta, "total": totales.get(e, 0), "results": results[e]}
        for e, etiqueta in Cotizacion.CHOICES_ESTATUS
    ]


# --- Opciones de filtros -----------------------------------------------------------


def opciones(qs):
    """Opciones del filtro de cliente sobre el conjunto visible completo (no reaccionan
    a filtros, como ``pedidos_listado.opciones``), en UNA consulta. La etiqueta es la
    razón social o, si está vacía, el nombre; "Sin cliente" va al final y sólo si
    alguna cotización visible no tiene cliente."""
    datos = qs.order_by().aggregate(
        clientes=ArrayAgg(
            JSONObject(
                value=F("cliente_id"),
                label=Coalesce(NullIf(Trim(F("cliente__razon_social")), Value("")), F("cliente__nombre")),
            ),
            distinct=True,
            filter=Q(cliente__isnull=False),
        ),
        sin_cliente=Count("id", filter=Q(cliente__isnull=True)),
    )
    clientes = sorted(
        ({"value": c["value"], "label": (c["label"] or "").strip()} for c in datos["clientes"] or []),
        key=lambda o: (clave_orden(o["label"]), o["value"]),
    )
    if datos["sin_cliente"]:
        clientes.append({"value": SIN_CLIENTE, "label": ETIQUETA_SIN_CLIENTE})
    return {"cliente": clientes}
