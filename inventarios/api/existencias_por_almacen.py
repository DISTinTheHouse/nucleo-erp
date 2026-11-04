"""Existencias de UN almacén para la pantalla de stock (EC-774).

``GET existencias/por-almacen/`` lista los renglones paginados y
``GET existencias/por-almacen/resumen/`` da los KPIs, el ``max_stock`` del
almacén y las opciones de los filtros de encabezado. El listado clásico
(``GET existencias/``) no cambia: estas acciones lo sustituyen solo en la
pantalla ``StockView`` del front (WMS, Compras y Producción).

Todo sale de ``.values()`` + anotaciones, sin instanciar modelos, y replica lo
que la pantalla hace hoy en memoria (``StockView.tsx``, ``StockColumns.tsx``,
``StockFilter.ts`` y ``utils/columnFilterOptions.ts`` del front):

- ``producto_info`` se resuelve igual que ``ExistenciaSerializer``: el
  ``producto`` de la fila o el de su variante; sin ninguno, el SKU de muestra
  (``variante_produccion``). ``sku``/``color`` salen de la variante (o de la
  muestra); ``tipo`` es el código del tipo del producto (las muestras no tienen).
- ``ubicacion_info.nombre_completo`` reproduce ``str(Ubicacion)``: el almacén
  es el de la UBICACIÓN, no el de la existencia (no siempre coinciden).
- Estado: ``stock / max_stock`` del almacén completo; > 80 % Óptimo (``full``),
  > 40 % Normal (``ok``), > 15 % Bajo (``low``), el resto Crítico
  (``critical``). ``max_stock`` = mayor ``stock`` del almacén, nunca menor a 0;
  con 0 todo es crítico. Se compara en enteros (``stock * 100 > max * 80``)
  para no depender del redondeo de un flotante.
"""

import re
import unicodedata

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import (
    BigIntegerField,
    CharField,
    Count,
    DateField,
    F,
    Max,
    Q,
    Subquery,
    Sum,
    Value,
    When,
    Case,
)
from django.db.models.functions import Cast, Coalesce, Concat, Greatest, JSONObject, Trunc
from django.db.models.lookups import GreaterThan, LessThanOrEqual
from rest_framework.exceptions import ValidationError

from nucleo.kpis import entero_param
from nucleo.listados import OrdenamientoEstableFilter

ESTADOS = ("full", "ok", "low", "critical")
# (estado, porcentaje mínimo EXCLUSIVO). ``critical`` es el resto.
_UMBRALES_ESTADO = (("full", 80), ("ok", 40), ("low", 15))

# ``matchesRangeFilter`` del front: ``min`` inclusivo, ``max`` exclusivo.
RANGOS_EXISTENCIAS = {
    "cero": {"stock__lt": 1},
    "1-10": {"stock__gte": 1, "stock__lt": 11},
    "11-100": {"stock__gte": 11, "stock__lt": 101},
    "mas-100": {"stock__gte": 101},
}

CAMPOS_ORDENAMIENTO = (
    "sku", "nombre", "tipo", "color", "stock", "fecha_actualizacion", "ubicacion_nombre", "id",
)

_MES_RE = re.compile(r"^(\d{4})-(\d{2})$")
_MESES = (
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
)

_ES_CATALOGO = Q(producto__isnull=False) | Q(producto_variante__isnull=False)
_ES_MUESTRA = Q(variante_produccion__isnull=False)


def _entero_positivo(request, nombre):
    """``entero_param`` (vacío → ``None``, no entero → 400) más positividad."""
    valor = entero_param(request, nombre)
    if valor is not None and valor <= 0:
        raise ValidationError({nombre: "Debe ser un entero positivo."})
    return valor


def almacen_id_requerido(request):
    """``?almacen_id=`` obligatorio y entero positivo; si no, 400."""
    almacen_id = _entero_positivo(request, "almacen_id")
    if almacen_id is None:
        raise ValidationError({"almacen_id": "Este parámetro es obligatorio."})
    return almacen_id


def _max_stock_del_almacen(base):
    """Mayor ``stock`` del almacén (sin filtros del cliente), nunca menor a 0.

    Subconsulta no correlacionada: PostgreSQL la evalúa una vez por consulta.
    """
    maximo = base.order_by().values("almacen_id").annotate(m=Max("stock")).values("m")[:1]
    return Greatest(Coalesce(Subquery(maximo), 0), 0)


def _estado(stock_x100, max_stock):
    return Case(
        *[
            When(GreaterThan(stock_x100, max_stock * pct), then=Value(estado))
            for estado, pct in _UMBRALES_ESTADO
        ],
        default=Value("critical"),
        output_field=CharField(),
    )


def anotar(base):
    """Campos de la pantalla como anotaciones planas (``nombre``, ``sku``,
    ``tipo``, ``color``, ``ubicacion_nombre``, ``max_stock``, ``estado``)."""
    ubicacion_nombre = Concat(
        Coalesce(F("ubicacion__almacen__nombre"), Value("(sin almacén)")),
        Value(" - "), F("ubicacion__pasillo"),
        Value("-"), F("ubicacion__rack"),
        Value("-"), F("ubicacion__nivel"),
        Value("-"), F("ubicacion__posicion"),
        output_field=CharField(),
    )
    return base.annotate(
        nombre=Case(
            When(producto__isnull=False, then=F("producto__nombre")),
            When(producto_variante__isnull=False, then=F("producto_variante__producto__nombre")),
            When(_ES_MUESTRA, then=F("variante_produccion__nombre")),
            default=None,
            output_field=CharField(),
        ),
        tipo=Case(
            When(producto__isnull=False, then=F("producto__tipo__codigo")),
            When(producto_variante__isnull=False, then=F("producto_variante__producto__tipo__codigo")),
            default=None,
            output_field=CharField(),
        ),
        sku=Case(
            When(_ES_CATALOGO, then=F("producto_variante__sku")),
            When(_ES_MUESTRA, then=F("variante_produccion__sku")),
            default=None,
            output_field=CharField(),
        ),
        color=Case(
            When(_ES_CATALOGO, then=F("producto_variante__color__nombre")),
            When(_ES_MUESTRA, then=F("variante_produccion__color__nombre")),
            default=None,
            output_field=CharField(),
        ),
        ubicacion_nombre=Case(
            When(ubicacion__isnull=True, then=None),
            default=ubicacion_nombre,
            output_field=CharField(),
        ),
        max_stock=_max_stock_del_almacen(base),
    ).annotate(
        estado=_estado(
            Cast("stock", BigIntegerField()) * 100, Cast("max_stock", BigIntegerField()),
        ),
    )


def _texto(request, nombre):
    return (request.query_params.get(nombre) or "").strip()


def filtrar(request, qs):
    """``search`` y un parámetro por cada filtro de encabezado. Valores fuera
    de catálogo (rango, estado, mes, ubicación) → 400."""
    buscar = _texto(request, "search")
    if buscar:
        qs = qs.filter(Q(nombre__icontains=buscar) | Q(sku__icontains=buscar))

    # Igualdad exacta sobre el valor mostrado, como ``exactFilter`` del front.
    for campo in ("nombre", "tipo", "color"):
        valor = request.query_params.get(campo)
        if valor:
            qs = qs.filter(**{campo: valor})

    ubicacion_id = _entero_positivo(request, "ubicacion")
    if ubicacion_id is not None:
        qs = qs.filter(ubicacion_id=ubicacion_id)

    rango = _texto(request, "existencias")
    if rango:
        if rango not in RANGOS_EXISTENCIAS:
            raise ValidationError({"existencias": f"Usa uno de: {', '.join(RANGOS_EXISTENCIAS)}."})
        qs = qs.filter(**RANGOS_EXISTENCIAS[rango])

    mes = _texto(request, "mes")
    if mes:
        coincide = _MES_RE.match(mes)
        if not coincide or not 1 <= int(coincide.group(2)) <= 12:
            raise ValidationError({"mes": "Formato AAAA-MM."})
        # ``__year``/``__month`` usan la zona del proyecto (America/Mexico_City),
        # la misma que el navegador de quien opera.
        qs = qs.filter(
            fecha_actualizacion__year=int(coincide.group(1)),
            fecha_actualizacion__month=int(coincide.group(2)),
        )

    estado = _texto(request, "estado")
    if estado:
        if estado not in ESTADOS:
            raise ValidationError({"estado": f"Usa uno de: {', '.join(ESTADOS)}."})
        qs = qs.filter(*_condiciones_estado(estado))
    return qs


def _condiciones_estado(estado):
    """El filtro de estado como desigualdades sobre ``stock`` y no como
    igualdad contra el ``CASE`` de ``estado``: con la igualdad el planificador
    estima ~1 fila, elige nested loops contra ``colores`` y, junto con
    ``search``, la página pasa de ~40 ms a ~200 ms (EXPLAIN ANALYZE, almacén de
    3,466 filas). Mismos cortes que ``_estado``."""
    stock_x100 = Cast("stock", BigIntegerField()) * 100
    maximo = Cast("max_stock", BigIntegerField())
    cortes = dict(_UMBRALES_ESTADO)
    piso = {"full": cortes["full"], "ok": cortes["ok"], "low": cortes["low"], "critical": None}[estado]
    techo = {"full": None, "ok": cortes["full"], "low": cortes["ok"], "critical": cortes["low"]}[estado]
    condiciones = []
    if piso is not None:
        condiciones.append(GreaterThan(stock_x100, maximo * piso))
    if techo is not None:
        condiciones.append(LessThanOrEqual(stock_x100, maximo * techo))
    return condiciones


class OrdenamientoPorAlmacenFilter(OrdenamientoEstableFilter):
    """``?ordering=`` con lista blanca propia (no la del viewset, que sigue
    siendo la del listado clásico), ``id`` como desempate y los NULL siempre al
    final: ~60 % de las existencias no tienen ``fecha_actualizacion`` y en
    PostgreSQL un DESC los pondría primero."""

    def get_valid_fields(self, queryset, view, context={}):
        return [(campo, campo) for campo in CAMPOS_ORDENAMIENTO]

    def get_default_ordering(self, view):
        return ["-fecha_actualizacion", "-id"]

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        return queryset.order_by(*[
            F(campo[1:]).desc(nulls_last=True) if campo.startswith("-") else F(campo).asc(nulls_last=True)
            for campo in ordering
        ])


def filas(qs):
    return qs.values(
        "id", "stock", "cantidad", "fecha_actualizacion", "estado",
        "nombre", "sku", "tipo", "color", "ubicacion_id", "ubicacion_nombre",
    )


def _clave_orden(texto):
    """Orden alfabético aproximado al ``localeCompare(..., "es")`` del front."""
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return (sin_acentos.casefold(), texto)


def _opciones(valores):
    """``buildDistinctFilterOptions``: valores distintos, sin vacíos ni
    etiquetas en blanco; ``value`` crudo y ``label`` recortada."""
    vistos = {v for v in (valores or []) if v is not None and v != "" and str(v).strip()}
    return sorted(({"value": v, "label": v.strip()} for v in vistos), key=lambda o: _clave_orden(o["label"]))


def resumen(qs):
    """KPIs del almacén completo (no reaccionan a filtros ni búsqueda, como
    ``StockStats`` hoy), ``max_stock`` y opciones de filtros, en UNA consulta."""
    datos = qs.aggregate(
        total_registros=Count("id"),
        skus_en_existencia=Count("id", filter=Q(stock__gt=0)),
        requiere_stock=Count("id", filter=Q(estado__in=["low", "critical"])),
        total_stock=Coalesce(Sum("stock"), 0),
        maximo=Greatest(Coalesce(Max("stock"), 0), 0),
        nombres=ArrayAgg("nombre", distinct=True),
        tipos=ArrayAgg("tipo", distinct=True),
        colores=ArrayAgg("color", distinct=True),
        ubicaciones=ArrayAgg(
            JSONObject(value=F("ubicacion_id"), label=F("ubicacion_nombre")),
            distinct=True,
            filter=Q(ubicacion__isnull=False),
        ),
        meses=ArrayAgg(
            Trunc("fecha_actualizacion", "month", output_field=DateField()),
            distinct=True,
            filter=Q(fecha_actualizacion__isnull=False),
        ),
    )
    ubicaciones = sorted(
        ({"value": u["value"], "label": u["label"].strip()} for u in datos["ubicaciones"] or [] if u["label"].strip()),
        key=lambda o: _clave_orden(o["label"]),
    )
    meses = [
        {"value": f"{m.year:04d}-{m.month:02d}", "label": f"{_MESES[m.month - 1]} {m.year}"}
        for m in sorted(datos["meses"] or [], reverse=True)
    ]
    return {
        "max_stock": datos["maximo"],
        "kpis": {
            "total_registros": datos["total_registros"],
            "skus_en_existencia": datos["skus_en_existencia"],
            "requiere_stock": datos["requiere_stock"],
            "total_stock": datos["total_stock"],
        },
        "opciones": {
            "nombre": _opciones(datos["nombres"]),
            "tipo": _opciones(datos["tipos"]),
            "color": _opciones(datos["colores"]),
            "ubicacion": ubicaciones,
            "mes": meses,
        },
    }
