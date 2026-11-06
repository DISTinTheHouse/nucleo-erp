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
  con 0 todo es crítico. Se calcula UNA vez por request (``max_stock()``, una
  consulta aparte) y entra como constante: un piso entero por estado
  (``stock > max * pct // 100``, equivalente exacto de ``stock / max > pct %``
  para ``stock`` entero), sin flotantes ni subconsultas repetidas.
"""

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Case, CharField, Count, DateField, F, Max, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Concat, JSONObject, Trunc
from rest_framework.exceptions import ValidationError

from nucleo.kpis import entero_param
from nucleo.opciones_filtros import clave_orden, mes_param, opciones_meses
from nucleo.listados import OrdenamientoEstableFilter

# (estado, porcentaje mínimo EXCLUSIVO), de mayor a menor; el último
# (``critical``) no tiene piso: es el resto. Única fuente de los cortes de la
# columna (``_estado``) y del filtro (``_condiciones_estado``).
_UMBRALES_ESTADO = (("full", 80), ("ok", 40), ("low", 15), ("critical", None))
ESTADOS = tuple(estado for estado, _ in _UMBRALES_ESTADO)

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


def max_stock(base):
    """Mayor ``stock`` del almacén (sin filtros del cliente), nunca menor a 0."""
    return max(base.aggregate(m=Max("stock"))["m"] or 0, 0)


def _umbrales(maximo):
    """``[(estado, piso)]`` en el orden de ``_UMBRALES_ESTADO``: el estado aplica
    con ``stock > piso`` (``None`` = sin piso). Para ``stock`` entero,
    ``stock > max * pct // 100`` equivale a ``stock * 100 > max * pct``."""
    return [(estado, None if pct is None else maximo * pct // 100) for estado, pct in _UMBRALES_ESTADO]


def _estado(maximo):
    return Case(
        *[When(stock__gt=piso, then=Value(estado)) for estado, piso in _umbrales(maximo) if piso is not None],
        default=Value(ESTADOS[-1]),
        output_field=CharField(),
    )


def anotar(base, maximo):
    """Campos de la pantalla como anotaciones planas (``nombre``, ``sku``,
    ``tipo``, ``color``, ``ubicacion_nombre``, ``estado``); ``maximo`` es el
    ``max_stock(base)`` del almacén."""
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
        estado=_estado(maximo),
    )


def _texto(request, nombre):
    return (request.query_params.get(nombre) or "").strip()


def filtrar(request, qs, maximo):
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

    mes = mes_param(request, "mes")
    if mes:
        # ``__year``/``__month`` usan la zona del proyecto (America/Mexico_City),
        # la misma que el navegador de quien opera.
        qs = qs.filter(fecha_actualizacion__year=mes[0], fecha_actualizacion__month=mes[1])

    estado = _texto(request, "estado")
    if estado:
        if estado not in ESTADOS:
            raise ValidationError({"estado": f"Usa uno de: {', '.join(ESTADOS)}."})
        qs = qs.filter(**_condiciones_estado(estado, maximo))
    return qs


def _condiciones_estado(estado, maximo):
    """El filtro de estado como rango sobre ``stock`` (``> piso`` propio y
    ``<= piso`` del estado de arriba), no como igualdad contra el ``CASE`` de
    ``estado``: con la igualdad el planificador estima ~1 fila, elige nested
    loops contra ``colores`` y, junto con ``search``, la página pasa de ~40 ms
    a ~200 ms (EXPLAIN ANALYZE, almacén de 3,466 filas)."""
    umbrales = _umbrales(maximo)
    i = ESTADOS.index(estado)
    condiciones = {}
    if umbrales[i][1] is not None:
        condiciones["stock__gt"] = umbrales[i][1]
    if i > 0:
        condiciones["stock__lte"] = umbrales[i - 1][1]
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


def _opciones(valores):
    """``buildDistinctFilterOptions``: valores distintos, sin vacíos ni
    etiquetas en blanco; ``value`` crudo y ``label`` recortada."""
    vistos = {v for v in (valores or []) if v is not None and v != "" and str(v).strip()}
    return sorted(({"value": v, "label": v.strip()} for v in vistos), key=lambda o: clave_orden(o["label"]))


def resumen(qs, maximo):
    """KPIs del almacén completo (no reaccionan a filtros ni búsqueda, como
    ``StockStats`` hoy) y opciones de filtros, en UNA consulta; ``maximo`` ya
    viene de ``max_stock()``."""
    datos = qs.aggregate(
        total_registros=Count("id"),
        skus_en_existencia=Count("id", filter=Q(stock__gt=0)),
        # low + critical = no supera el piso de ``ok``.
        requiere_stock=Count("id", filter=Q(stock__lte=dict(_umbrales(maximo))["ok"])),
        total_stock=Coalesce(Sum("stock"), 0),
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
        key=lambda o: clave_orden(o["label"]),
    )
    meses = opciones_meses(datos["meses"])
    return {
        "max_stock": maximo,
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
