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

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Case, CharField, Count, DecimalField, F, IntegerField, Q, Sum, Value, When, Window
from django.db.models.functions import Coalesce, Collate, NullIf, Round, RowNumber, Trim
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import ValidationError

from nucleo.kpis import entero_param, rango_fechas
from nucleo.listados import OrdenamientoEstableFilter, estatus_param
from nucleo.opciones_filtros import clave_orden
from terceros.models import Cliente
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
# Parámetros que lee ``filtrar``: ÚNICA lista. De ella salen el schema del listado,
# ``kpis/`` y ``tablero/`` (``PARAMETROS_FILTRO_OPENAPI``) y lo que esas dos acciones
# aceptan (``PARAMETROS_FILTRO``). Un filtro nuevo en ``filtrar`` va aquí; si no,
# ``CotizacionParametrosFiltroTests`` falla.
PARAMETROS_FILTRO_OPENAPI = (
    OpenApiParameter("q", OpenApiTypes.STR, description=(
        "Contiene, en OC y nombre, razón social o RFC del cliente; si son dígitos, también el id exacto."
    )),
    OpenApiParameter("estatus", OpenApiTypes.STR, description="Uno o varios estatus (1-5) separados por coma."),
    OpenApiParameter("cliente", OpenApiTypes.STR, description=(
        f"Id del cliente, o `{SIN_CLIENTE}` para las cotizaciones sin cliente."
    )),
    OpenApiParameter("vendedor", OpenApiTypes.INT, description="Id del vendedor."),
    OpenApiParameter("fecha_desde", OpenApiTypes.DATE, description="Día local de creación, desde (YYYY-MM-DD), inclusivo."),
    OpenApiParameter("fecha_hasta", OpenApiTypes.DATE, description="Día local de creación, hasta (YYYY-MM-DD), inclusivo."),
)
PARAMETROS_FILTRO = frozenset(p.name for p in PARAMETROS_FILTRO_OPENAPI)


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
    if fecha_hasta and fecha_hasta >= date.max:
        # El límite superior es la medianoche del día siguiente: no existe para
        # ``date.max`` (``9999-12-31`` + 1 día desborda).
        raise ValidationError({"fecha_hasta": "Fecha fuera de rango; usa una anterior a 9999-12-31."})
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
        # Sólo dígitos ASCII: ``str.isdigit`` también acepta "²" o "①", que ``int()``
        # rechaza (500).
        if q.isascii() and q.isdigit():
            q_filter = q_filter | Q(id=int(q))
        qs = qs.filter(q_filter)
    return qs


# --- KPIs --------------------------------------------------------------------------


def _importe(valor):
    """Texto con dos decimales, como ``gran_total`` en el listado."""
    return str((valor or Decimal("0")).quantize(CENTAVOS))


def kpis(qs):
    """Conteos por estatus y ``gran_total`` por moneda del conjunto filtrado, en UNA
    consulta agrupada por ``(estatus, código de moneda)``. ``por_estatus`` trae siempre los 5
    estatus del catálogo (en cero si no hay); una fila con estatus fuera del catálogo
    no tiene bloque pero cuenta en ``total`` y ``por_moneda`` (``total`` = ``count``
    del listado con los mismos filtros); la moneda nula es un grupo más
    (``moneda`` en ``null``) y va al final. Por código y no por ``moneda_id``: cada
    empresa tiene su propia ``Moneda``, y un superusuario recibía "MXN" repetido.
    ``order_by()`` antes de agrupar: el orden por defecto del listado entraría al
    ``GROUP BY``."""
    filas = list(
        qs.order_by()
        .values("estatus", "moneda__codigo_iso")
        .annotate(total=Count("id"), gran_total=Sum("gran_total"))
        .order_by("moneda__codigo_iso", "estatus")
    )
    por_moneda = {}
    por_estatus = {e: {"total": 0, "por_moneda": []} for e in ESTATUS_VALIDOS}
    for f in filas:
        moneda = {"moneda": f["moneda__codigo_iso"]}
        acumulado = por_moneda.setdefault(f["moneda__codigo_iso"], {**moneda, "total": 0, "gran_total": Decimal("0")})
        acumulado["total"] += f["total"]
        acumulado["gran_total"] += f["gran_total"] or Decimal("0")
        bloque = por_estatus.get(f["estatus"])
        if bloque is None:
            # Estatus fuera del catálogo (la columna no tiene CheckConstraint): no
            # tiene bloque, pero sí cuenta en ``total`` y ``por_moneda``, igual que en
            # el ``count`` del listado con los mismos filtros.
            continue
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
    # Con signo (EC-829): ``-1`` llega al rango y responde su mensaje, como ``0``.
    valor = entero_param(request, "por_columna", con_signo=True)
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
    estatus sin filas no aparece, ni uno fuera del catálogo (no tiene columna)."""
    return list(
        qs.filter(estatus__in=ESTATUS_VALIDOS)
        .annotate(
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
        if estatus not in results:
            continue  # fuera del catálogo: sin columna (``ventana_tablero`` ya los excluye)
        totales[estatus] = total
        if pk in filas_serializadas:
            results[estatus].append(filas_serializadas[pk])
    return [
        {"estatus": e, "estatus_label": etiqueta, "total": totales.get(e, 0), "results": results[e]}
        for e, etiqueta in Cotizacion.CHOICES_ESTATUS
    ]


# --- Ordenamiento ------------------------------------------------------------------

CAMPO_ORDEN_IMPORTE = "importe_sin_iva"

# El mismo importe que pinta el listado (``serializers.importes_iva``): ``gran_total``
# sin el IVA, redondeado a centavos; con ``iva`` <= 0, ``gran_total`` tal cual.
# ``importes_iva`` redondea con ``ROUND_HALF_EVEN`` y Postgres a la mitad hacia
# arriba: sólo difieren en un empate exacto de medio centavo, que sólo existe cuando
# ``100 + iva`` es múltiplo de 8 (IVA 4, 12, 20, 100…; nunca con 0, 8, 16 u 80, las
# tasas que hay en la base). ``CotizacionImporteSinIvaSqlTests`` compara las dos. ``gran_total * 100 / (100 + iva)``
# y no ``gran_total / (1 + iva / 100)``: en Postgres ``iva / 100`` es división entera (0).
IMPORTE_SIN_IVA = Case(
    When(
        iva__gt=0,
        then=Round(F("gran_total") * Value(Decimal("100")) / (Value(100) + F("iva")), 2),
    ),
    default=F("gran_total"),
    output_field=DecimalField(max_digits=14, decimal_places=2),
)
# Código de la moneda; vacío cuenta como sin moneda (``moneda_codigo || null`` del
# front). Collation ``C``: el front compara con ``<`` de JavaScript (unidades de
# código), no con el orden lingüístico de la base.
MONEDA_ORDEN = Collate(NullIf(F("moneda__codigo_iso"), Value("")), "C")


def _orden(campo):
    """Expresiones de ``order_by`` de un término de ``?ordering=``.

    ``importe_sin_iva`` reproduce ``compareQuoteAmounts`` + TanStack Table del front
    (``quoteAmountSorting.ts``): primero la moneda (código ascendente, sin moneda al
    final) y dentro de ella el importe. En descendente TanStack invierte la
    comparación completa: códigos al revés, sin moneda PRIMERO e importe de mayor a
    menor. El desempate por ``id`` lo agrega ``OrdenamientoEstableFilter``."""
    if campo.lstrip("-") != CAMPO_ORDEN_IMPORTE:
        return [campo]
    if campo.startswith("-"):
        return [F("orden_moneda").desc(nulls_first=True), F("orden_importe_sin_iva").desc()]
    return [F("orden_moneda").asc(nulls_last=True), F("orden_importe_sin_iva").asc()]


class OrdenamientoCotizacionesFilter(OrdenamientoEstableFilter):
    """``?ordering=`` del listado de cotizaciones (y de mesa de control): la lista
    blanca de la vista, con ``id`` de desempate, y ``importe_sin_iva`` agrupado por
    moneda. Las dos columnas de orden se anotan sólo cuando se piden."""

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset
        if any(campo.lstrip("-") == CAMPO_ORDEN_IMPORTE for campo in ordering):
            queryset = queryset.annotate(orden_moneda=MONEDA_ORDEN, orden_importe_sin_iva=IMPORTE_SIN_IVA)
        return queryset.order_by(*[expr for campo in ordering for expr in _orden(campo)])


# --- Opciones de filtros -----------------------------------------------------------


def opciones(qs):
    """Opciones del filtro de cliente sobre el conjunto visible completo (no reaccionan
    a filtros, como ``pedidos_listado.opciones``), en UNA consulta. La etiqueta es la
    razón social o, si está vacía, el nombre; "Sin cliente" va al final y sólo si
    alguna cotización visible no tiene cliente.

    Primero se deduplican los clientes (``Cliente`` por ``pk__in`` de los
    ``cliente_id`` visibles) y sólo entonces se arma la etiqueta, una por cliente y
    no una por cotización. "Sin cliente" va en la misma consulta (``UNION ALL`` con
    una fila de marca, ``[:1]``: basta con que exista una)."""
    visibles = qs.order_by()
    clientes = (
        Cliente.objects.filter(pk__in=visibles.filter(cliente__isnull=False).values("cliente_id"))
        .order_by()
        .values_list("pk", Coalesce(NullIf(Trim(F("razon_social")), Value("")), F("nombre")))
    )
    marca_sin_cliente = (
        visibles.filter(cliente__isnull=True)
        .values_list(Value(None, output_field=IntegerField()), Value(ETIQUETA_SIN_CLIENTE, output_field=CharField()))[:1]
    )
    filas = list(clientes.union(marca_sin_cliente, all=True))
    opciones_cliente = sorted(
        ({"value": pk, "label": (label or "").strip()} for pk, label in filas if pk is not None),
        key=lambda o: (clave_orden(o["label"]), o["value"]),
    )
    if any(pk is None for pk, _ in filas):
        opciones_cliente.append({"value": SIN_CLIENTE, "label": ETIQUETA_SIN_CLIENTE})
    return {"cliente": opciones_cliente}
