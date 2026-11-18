"""Listado de pickings en modo servidor (EC-921): filtros, búsqueda, orden y KPIs.

Lo consume WMS › Surtido (``PickingView`` del front, EC-915), que hoy baja la lista
completa, filtra y ordena en el navegador y calcula sus tarjetas con
``computePickingKpis`` sobre lo cargado.

Los filtros van SIEMPRE encima del alcance (``documentos_wms_visibles``, vía
``PickingViewSet.get_queryset``): sólo acotan lo que el usuario ya ve. Sólo en
``ACCIONES_FILTRADAS`` (el listado y sus KPIs); las acciones de detalle resuelven
por pk sin ellos, como ``cotizaciones_listado``.
"""

from django.db.models import Case, CharField, Count, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce, Concat, Greatest, NullIf, Trim
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import ValidationError
from rest_framework.filters import SearchFilter

from nucleo.listados import OrdenamientoEstableFilter
from wms.models import Picking

ACCION_KPIS = "kpis"
ACCIONES_FILTRADAS = ("list", ACCION_KPIS)

ESTADOS_VALIDOS = frozenset(Picking.Estado.values)
PRIORIDADES_VALIDAS = frozenset(Picking.Prioridad.values)

# De la más urgente a la menos: orden de ``por_prioridad`` en los KPIs, el mismo que
# ``PICKING_PRIORIDAD_ORDER`` del front.
PRIORIDADES_POR_URGENCIA = (Picking.Prioridad.ALTA, Picking.Prioridad.MEDIA, Picking.Prioridad.BAJA)

PARAMETROS_FILTRO_OPENAPI = (
    OpenApiParameter("search", OpenApiTypes.STR, description=(
        "Contiene, en el folio del picking, el folio del pedido o el nombre del operador "
        "(nombre, apellido o correo). Varias palabras: cada una debe coincidir en algún campo."
    )),
    OpenApiParameter("estado", OpenApiTypes.STR, description=(
        "Uno o varios estatus separados por coma: " + ", ".join(Picking.Estado.values) + "."
    )),
    OpenApiParameter("prioridad", OpenApiTypes.STR, description=(
        "Una o varias prioridades separadas por coma: " + ", ".join(Picking.Prioridad.values) + "."
    )),
)
PARAMETRO_RESUMEN_OPENAPI = OpenApiParameter("resumen", OpenApiTypes.BOOL, description=(
    "`1`/`true`/`si`: cada picking sin `picking_detalle` ni `ordenes_trabajo` (#1149). "
    "Sin él, el listado anida los renglones como el detalle."
))


# --- Filtros ---------------------------------------------------------------------


def _valores_param(request, nombre, validos, mensaje):
    """``?{nombre}=A`` o ``?{nombre}=A,B`` → lista; vacío → ``None``. Un valor fuera
    de ``validos`` (las ``choices`` del modelo, sensible a mayúsculas) → 400, como
    ``estatus_param`` en los listados de Compras y Ventas."""
    valor = (request.query_params.get(nombre) or "").strip()
    if not valor:
        return None
    lista = [parte.strip() for parte in valor.split(",") if parte.strip()]
    if not lista or any(v not in validos for v in lista):
        raise ValidationError({nombre: mensaje})
    return lista


def filtrar(request, qs):
    """``?estado=`` y ``?prioridad=`` (uno o varios, separados por coma). Inválidos → 400.
    ``?search=`` lo aplica ``BusquedaPickingsFilter`` con los ``search_fields`` de la vista."""
    estados = _valores_param(request, "estado", ESTADOS_VALIDOS, "Estado inexistente.")
    if estados is not None:
        qs = qs.filter(estado__in=estados)
    prioridades = _valores_param(request, "prioridad", PRIORIDADES_VALIDAS, "Prioridad inexistente.")
    if prioridades is not None:
        qs = qs.filter(prioridad__in=prioridades)
    return qs


class BusquedaPickingsFilter(SearchFilter):
    """``?search=`` sólo en ``ACCIONES_FILTRADAS``. ``get_object()`` también pasa por
    los filter backends: sin esto, ``GET /pickings/{id}/?search=x`` (o ``trasladar``)
    respondía 404 sobre un picking visible."""

    def get_search_fields(self, view, request):
        if getattr(view, "action", None) not in ACCIONES_FILTRADAS:
            return None
        return super().get_search_fields(view, request)


# --- Ordenamiento ----------------------------------------------------------------

# Nombres de ``?ordering=`` que no son columnas: el nombre de la columna del front
# (``pedido_folio``, ``operador_nombre``) o una anotación (``prioridad`` por
# urgencia). El resto de ``ordering_fields`` son columnas de ``Picking``.
CAMPO_PEDIDO_FOLIO = "pedido_folio"
CAMPO_OPERADOR_NOMBRE = "operador_nombre"
CAMPO_PRIORIDAD = "prioridad"
# ``ordering_fields`` de ``PickingViewSet``: las columnas de la tabla del front y las
# fechas y totales del picking.
CAMPOS_ORDEN = (
    "id", "folio", CAMPO_PEDIDO_FOLIO, CAMPO_OPERADOR_NOMBRE, "estado", CAMPO_PRIORIDAD,
    "created_at", "fecha_limite", "total_lineas",
)
PARAMETRO_ORDERING_OPENAPI = OpenApiParameter("ordering", OpenApiTypes.STR, description=(
    "Campo de orden, `-` para descendente; varios separados por coma: " + ", ".join(CAMPOS_ORDEN) + ". "
    "`prioridad` va por urgencia (BAJA → MEDIA → ALTA; `-prioridad`, ALTA primero); "
    "`pedido_folio` deja los pedidos sin folio al final. Desempate por `id`. "
    "Sin él (o con un campo fuera de la lista), más reciente primero."
))

# Peso de urgencia, el de ``PICKING_PRIORIDAD_RANK`` del front: ascendente es
# BAJA → MEDIA → ALTA; un valor fuera del catálogo pesa 0, como su ``?? 0``.
PRIORIDAD_RANGO = Case(
    When(prioridad=Picking.Prioridad.ALTA, then=Value(3)),
    When(prioridad=Picking.Prioridad.MEDIA, then=Value(2)),
    When(prioridad=Picking.Prioridad.BAJA, then=Value(1)),
    default=Value(0),
    output_field=IntegerField(),
)

# ``operador_nombre`` del serializer (``get_full_name().strip() or email``): nombre
# completo o, si queda vacío, el correo. Mismo armado que ``vendedor_nombre`` de
# ``pedidos_listado``.
OPERADOR_NOMBRE = Coalesce(
    NullIf(
        Trim(Concat(
            Coalesce(F("operador__first_name"), Value("")),
            Value(" "),
            Coalesce(F("operador__last_name"), Value("")),
        )),
        Value(""),
    ),
    F("operador__email"),
    output_field=CharField(),
)

_ANOTACIONES_ORDEN = {
    CAMPO_PRIORIDAD: ("orden_prioridad", PRIORIDAD_RANGO),
    CAMPO_OPERADOR_NOMBRE: ("orden_operador_nombre", OPERADOR_NOMBRE),
}


def _termino(campo):
    """Expresión de ``order_by`` de un término de ``?ordering=``. ``pedido_folio``
    (``Pedido.folio`` es NULL-able) deja los vacíos al final en ambas direcciones,
    como ``OrdenamientoNullsAlFinal``."""
    descendente = campo.startswith("-")
    nombre = campo.lstrip("-")
    if nombre == CAMPO_PEDIDO_FOLIO:
        expr = F("pedido__folio")
        return expr.desc(nulls_last=True) if descendente else expr.asc(nulls_last=True)
    if nombre in _ANOTACIONES_ORDEN:
        expr = F(_ANOTACIONES_ORDEN[nombre][0])
        return expr.desc() if descendente else expr.asc()
    return campo


class OrdenamientoPickingsFilter(OrdenamientoEstableFilter):
    """``?ordering=`` del listado de pickings: la lista blanca de la vista con ``id``
    de desempate (``OrdenamientoEstableFilter``), ``prioridad`` por urgencia y no
    alfabético, y ``pedido_folio``/``operador_nombre`` con el nombre de la columna
    del front. Las anotaciones se agregan sólo cuando se piden; sin ``?ordering=``
    válido queda el orden de ``get_queryset`` (más reciente primero)."""

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset
        pedidos = {campo.lstrip("-") for campo in ordering}
        anotaciones = {alias: expr for nombre, (alias, expr) in _ANOTACIONES_ORDEN.items() if nombre in pedidos}
        if anotaciones:
            queryset = queryset.annotate(**anotaciones)
        return queryset.order_by(*[_termino(campo) for campo in ordering])


# --- KPIs ------------------------------------------------------------------------


def kpis(qs):
    """Tarjetas del listado sobre el conjunto filtrado, en UNA consulta (``aggregate``).

    Misma definición que ``computePickingKpis`` del front:

    - ``total``: pickings del conjunto, cualquiera que sea su estado o prioridad
      (= ``count`` del listado con los mismos filtros).
    - ``lineas_por_surtir``: suma de ``total_lineas - total_lineas_completas`` de cada
      picking, cada término acotado a ``>= 0``.
    - ``por_prioridad``: pickings por prioridad, siempre las tres del catálogo de la
      más urgente a la menos (en cero si no hay); una prioridad fuera del catálogo
      cuenta en ``total`` pero en ningún bloque.
    """
    etiquetas = dict(Picking.Prioridad.choices)
    datos = qs.order_by().aggregate(
        total=Count("id"),
        lineas_por_surtir=Coalesce(
            Sum(Greatest(F("total_lineas") - F("total_lineas_completas"), Value(0))), Value(0)
        ),
        **{f"prioridad_{p.value}": Count("id", filter=Q(prioridad=p.value)) for p in PRIORIDADES_POR_URGENCIA},
    )
    return {
        "total": datos["total"],
        "lineas_por_surtir": datos["lineas_por_surtir"],
        "por_prioridad": [
            {"prioridad": p.value, "prioridad_label": etiquetas[p.value], "total": datos[f"prioridad_{p.value}"]}
            for p in PRIORIDADES_POR_URGENCIA
        ],
    }
