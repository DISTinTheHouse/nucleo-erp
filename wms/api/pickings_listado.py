"""Listado de pickings en modo servidor (EC-921): filtros, búsqueda, orden y KPIs.

Lo consume WMS › Surtido (``PickingView`` del front, EC-915), que hoy baja la lista
completa, filtra y ordena en el navegador y calcula sus tarjetas con
``computePickingKpis`` sobre lo cargado.

Los filtros van SIEMPRE encima del alcance (``documentos_wms_visibles``, vía
``PickingViewSet.get_queryset``): sólo acotan lo que el usuario ya ve. Sólo en
``ACCIONES_FILTRADAS`` (el listado y sus KPIs); las acciones de detalle resuelven
por pk sin ellos, como ``cotizaciones_listado``.
"""

from datetime import timedelta

from django.db.models import (
    Case, CharField, Count, DateField, DecimalField, Exists, ExpressionWrapper, F, IntegerField, OuterRef, Q, Sum,
    Value, When,
)
from django.db.models import Subquery
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.db.models.functions import Coalesce, Greatest
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import ValidationError
from rest_framework.filters import SearchFilter

from nucleo.listados import OrdenamientoEstableFilter, nombre_para_mostrar_sql

from ventas.services.clasificacion_service import _RANGOS_DIAS, rango_fecha_entrega
from wms.models import Despacho, Packing, Picking, PickingDetalle, Transferencia

ACCION_KPIS = "kpis"
ACCION_RESUMEN = "resumen"
ACCIONES_FILTRADAS = ("list", ACCION_KPIS, ACCION_RESUMEN)

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
    OpenApiParameter("programacion_destino", OpenApiTypes.STR, description=(
        "Uno o varios destinos separados por coma: " + ", ".join(Picking.ProgramacionDestino.values) + "."
    )),
    OpenApiParameter("almacen_actual", OpenApiTypes.INT, description=(
        "Id del almacén donde están hoy las prendas (`almacen_actual`)."
    )),
    OpenApiParameter("estado_empaque", OpenApiTypes.STR, description=(
        "Uno o varios separados por coma: POR_EMPACAR, EMPACADO, ENVIADO."
    )),
    OpenApiParameter("cliente", OpenApiTypes.INT, description="Id del cliente del pedido."),
    OpenApiParameter("operador", OpenApiTypes.INT, description="Id del operador del picking."),
    OpenApiParameter("pedido_fecha_entrega_desde", OpenApiTypes.DATE, description="Entrega del pedido desde."),
    OpenApiParameter("pedido_fecha_entrega_hasta", OpenApiTypes.DATE, description="Entrega del pedido hasta, inclusive."),
    OpenApiParameter("fecha_desde", OpenApiTypes.DATE, description="Alta del picking desde (AAAA-MM-DD)."),
    OpenApiParameter("fecha_hasta", OpenApiTypes.DATE, description="Alta del picking hasta (AAAA-MM-DD), inclusive."),
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
    ``?search=`` lo aplica ``BusquedaPickingsFilter`` con los ``search_fields`` de la vista.

    Anota antes ``piezas``/``productos``/``tallas``/``estado_empaque`` (``anotar``): los
    usan filtros, orden, la vista de lista y el ``resumen``."""
    qs = anotar(qs)
    estados_empaque = _valores_param(
        request, "estado_empaque", frozenset(ESTADOS_EMPAQUE), "Estado de empaque inexistente."
    )
    if estados_empaque is not None:
        qs = qs.filter(estado_empaque__in=estados_empaque)
    for nombre, campo in (("cliente", "pedido__cliente_id"), ("operador", "operador_id")):
        valor = (request.query_params.get(nombre) or "").strip()
        if valor:
            if not valor.isdigit():
                raise ValidationError({nombre: "Debe ser un id."})
            qs = qs.filter(**{campo: int(valor)})
    entrega = {
        nombre: (request.query_params.get(nombre) or "").strip()
        for nombre in ("pedido_fecha_entrega_desde", "pedido_fecha_entrega_hasta")
    }
    if any(entrega.values()):
        qs = qs.annotate(orden_fecha_entrega=FECHA_ENTREGA)
        for nombre, lookup in (("pedido_fecha_entrega_desde", "gte"), ("pedido_fecha_entrega_hasta", "lte")):
            if entrega[nombre]:
                fecha = parse_date(entrega[nombre])
                if fecha is None:
                    raise ValidationError({nombre: "Fecha inválida (AAAA-MM-DD)."})
                qs = qs.filter(**{f"orden_fecha_entrega__{lookup}": fecha})
    estados = _valores_param(request, "estado", ESTADOS_VALIDOS, "Estado inexistente.")
    if estados is not None:
        qs = qs.filter(estado__in=estados)
    prioridades = _valores_param(request, "prioridad", PRIORIDADES_VALIDAS, "Prioridad inexistente.")
    if prioridades is not None:
        qs = qs.filter(prioridad__in=prioridades)
    destinos = _valores_param(
        request, "programacion_destino", frozenset(Picking.ProgramacionDestino.values), "Destino inexistente."
    )
    if destinos is not None:
        qs = qs.filter(programacion_destino__in=destinos)
    almacen = (request.query_params.get("almacen_actual") or "").strip()
    if almacen:
        if not almacen.isdigit():
            raise ValidationError({"almacen_actual": "Debe ser un id de almacén."})
        # Misma regla que ``Picking.almacen_actual``: el destino si la TR ya se completó.
        movido = Q(transferencia__isnull=False, almacen_destino__isnull=False,
                   transferencia__status=Transferencia.TransferenciaStatus.COMPLETADA)
        qs = qs.filter((movido & Q(almacen_destino_id=int(almacen))) | (~movido & Q(almacen_id=int(almacen))))
    for nombre, lookup in (("fecha_desde", "created_at__date__gte"), ("fecha_hasta", "created_at__date__lte")):
        valor = (request.query_params.get(nombre) or "").strip()
        if valor:
            fecha = parse_date(valor)
            if fecha is None:
                raise ValidationError({nombre: "Fecha inválida (AAAA-MM-DD)."})
            qs = qs.filter(**{lookup: fecha})
    return qs


# Anotación de ``search_fields`` con el operador tal como lo pinta la fila
# (``OPERADOR_NOMBRE``): el correo sólo cuenta cuando el operador no tiene nombre.
CAMPO_BUSQUEDA_OPERADOR = "busqueda_operador_nombre"


class BusquedaPickingsFilter(SearchFilter):
    """``?search=`` sólo en ``ACCIONES_FILTRADAS``. ``get_object()`` también pasa por
    los filter backends: sin esto, ``GET /pickings/{id}/?search=x`` (o ``trasladar``)
    respondía 404 sobre un picking visible.

    Anota ``CAMPO_BUSQUEDA_OPERADOR`` sólo cuando hay búsqueda; ``SearchFilter`` acepta
    anotaciones en ``search_fields`` (``icontains`` sobre la expresión)."""

    def get_search_fields(self, view, request):
        if getattr(view, "action", None) not in ACCIONES_FILTRADAS:
            return None
        return super().get_search_fields(view, request)

    def filter_queryset(self, request, queryset, view):
        if self.get_search_fields(view, request) and self.get_search_terms(request):
            queryset = queryset.annotate(**{CAMPO_BUSQUEDA_OPERADOR: OPERADOR_NOMBRE})
        return super().filter_queryset(request, queryset, view)


# --- Ordenamiento ----------------------------------------------------------------

# Nombres de ``?ordering=`` que no son columnas: el nombre de la columna del front
# (``pedido_folio``, ``operador_nombre``) o una anotación (``prioridad`` por
# urgencia). El resto de ``ordering_fields`` son columnas de ``Picking``.
CAMPO_PEDIDO_FOLIO = "pedido_folio"
CAMPO_OPERADOR_NOMBRE = "operador_nombre"
CAMPO_PRIORIDAD = "prioridad"
CAMPO_FECHA_ENTREGA = "pedido_fecha_entrega"
CAMPO_ESTADO_EMPAQUE = "estado_empaque"
# ``ordering_fields`` de ``PickingViewSet``: las columnas de la tabla del front y las
# fechas y totales del picking.
CAMPOS_ORDEN = (
    "id", "folio", CAMPO_PEDIDO_FOLIO, CAMPO_OPERADOR_NOMBRE, "estado", CAMPO_PRIORIDAD,
    "created_at", "fecha_limite", "total_lineas", CAMPO_FECHA_ENTREGA, "piezas", CAMPO_ESTADO_EMPAQUE,
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
OPERADOR_NOMBRE = nombre_para_mostrar_sql("operador")

# ``pedido_fecha_entrega`` en SQL: ``fecha_clasificacion`` + días máximos de la
# clasificación (``rango_fecha_entrega``); sin clasificación o ``X``, NULL.
FECHA_ENTREGA = Case(
    *[
        When(pedido__clasificacion=clave, then=ExpressionWrapper(
            F("pedido__fecha_clasificacion") + timedelta(days=dias_max), output_field=DateField()
        ))
        for clave, (_dias_min, dias_max) in _RANGOS_DIAS.items()
    ],
    default=Value(None),
    output_field=DateField(),
)

_ANOTACIONES_ORDEN = {
    CAMPO_FECHA_ENTREGA: ("orden_fecha_entrega", FECHA_ENTREGA),
    # Del más atrasado al más avanzado (``anotar`` deja ``estado_empaque``).
    CAMPO_ESTADO_EMPAQUE: ("orden_estado_empaque", None),
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
        if nombre == CAMPO_FECHA_ENTREGA:  # sin fecha, al final
            return expr.desc(nulls_last=True) if descendente else expr.asc(nulls_last=True)
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
        anotaciones = {
            alias: (RANGO_EMPAQUE if nombre == CAMPO_ESTADO_EMPAQUE else expr)
            for nombre, (alias, expr) in _ANOTACIONES_ORDEN.items() if nombre in pedidos
        }
        if anotaciones:
            queryset = queryset.annotate(**anotaciones)
        return queryset.order_by(*[_termino(campo) for campo in ordering])


# --- Datos por fila en bloque ----------------------------------------------------

ESTADO_POR_EMPACAR = "POR_EMPACAR"
ESTADO_EMPACADO = "EMPACADO"
ESTADO_ENVIADO = "ENVIADO"
ESTADOS_EMPAQUE = (ESTADO_POR_EMPACAR, ESTADO_EMPACADO, ESTADO_ENVIADO)


def _detalle_vivo():
    return PickingDetalle.objects.filter(picking=OuterRef("pk")).exclude(
        estado=PickingDetalle.EstadoLinea.CANCELADA
    )


def _agregado(expr, output_field):
    """Subconsulta escalar sobre los renglones vivos del picking (por FK indexada)."""
    return Coalesce(
        Subquery(_detalle_vivo().order_by().values("picking").annotate(v=expr).values("v")[:1]),
        Value(0),
        output_field=output_field,
    )


# Misma regla que ``estado_empaque()`` (fila) en SQL, para filtrar, ordenar y resumir.
_PACKINGS_VIVOS = Packing.objects.filter(picking=OuterRef("pk")).exclude(estado="CANCELADO")
ESTADO_EMPAQUE_SQL = Case(
    When(Exists(Despacho.objects.filter(packing__picking=OuterRef("pk")).exclude(packing__estado="CANCELADO")),
         then=Value(ESTADO_ENVIADO)),
    When(Exists(_PACKINGS_VIVOS), then=Value(ESTADO_EMPACADO)),
    default=Value(ESTADO_POR_EMPACAR),
    output_field=CharField(),
)
RANGO_EMPAQUE = Case(
    When(estado_empaque=ESTADO_POR_EMPACAR, then=Value(1)),
    When(estado_empaque=ESTADO_EMPACADO, then=Value(2)),
    default=Value(3),
    output_field=IntegerField(),
)


def anotar(qs):
    """``piezas`` (Σ ``cantidad_asignada``), ``productos`` (renglones de pedido
    distintos = producto + color), ``tallas`` (líneas) y ``estado_empaque``, sin
    contar renglones cancelados. Subconsultas por FK indexada: no crece con las filas."""
    return qs.annotate(
        piezas=_agregado(Sum("cantidad_asignada"), DecimalField(max_digits=18, decimal_places=4)),
        productos=_agregado(Count("pedido_detalle", distinct=True), IntegerField()),
        tallas=_agregado(Count("id"), IntegerField()),
        estado_empaque=ESTADO_EMPAQUE_SQL,
    )


def resumen(qs):
    """Indicadores sobre TODO el conjunto filtrado (no la página): 2 consultas.

    - ``por_empacar``: surtidos no cancelados sin packing y sus piezas.
    - ``surtidos_hoy``: surtidos dados de alta hoy (hora de México), piezas y pedidos.
    - ``piezas_por_destino``: piezas por ``programacion_destino`` (``null`` = sin destino).
    """
    vivos = qs.exclude(estado=Picking.Estado.CANCELADO).order_by()
    hoy = timezone.localdate()
    datos = vivos.aggregate(
        por_empacar_surtidos=Count("id", filter=Q(estado_empaque=ESTADO_POR_EMPACAR)),
        por_empacar_piezas=Coalesce(Sum("piezas", filter=Q(estado_empaque=ESTADO_POR_EMPACAR)), Value(0), output_field=DecimalField()),
        hoy_surtidos=Count("id", filter=Q(created_at__date=hoy)),
        hoy_piezas=Coalesce(Sum("piezas", filter=Q(created_at__date=hoy)), Value(0), output_field=DecimalField()),
        hoy_pedidos=Count("pedido", filter=Q(created_at__date=hoy), distinct=True),
    )
    por_destino = (
        PickingDetalle.objects.filter(picking__in=vivos.values("pk"))
        .exclude(estado=PickingDetalle.EstadoLinea.CANCELADA)
        .values("picking__programacion_destino")
        .annotate(piezas=Sum("cantidad_asignada"))
        .order_by("picking__programacion_destino")
    )
    return {
        "por_empacar": {"surtidos": datos["por_empacar_surtidos"], "piezas": float(datos["por_empacar_piezas"])},
        "surtidos_hoy": {
            "fecha": hoy,
            "surtidos": datos["hoy_surtidos"],
            "piezas": float(datos["hoy_piezas"]),
            "pedidos": datos["hoy_pedidos"],
        },
        "piezas_por_destino": [
            {"destino": f["picking__programacion_destino"], "piezas": float(f["piezas"] or 0)} for f in por_destino
        ],
    }


def catalogos_filtros(qs):
    """Clientes y operadores con al menos un surtido visible (sin filtros): 2 consultas."""
    clientes = (
        qs.order_by().filter(pedido__cliente__isnull=False)
        .values("pedido__cliente_id", "pedido__cliente__nombre").distinct()
    )
    operadores = qs.order_by().values(
        "operador_id", "operador__first_name", "operador__last_name", "operador__email"
    ).distinct()
    return {
        "clientes": sorted(
            ({"id": c["pedido__cliente_id"], "nombre": c["pedido__cliente__nombre"]} for c in clientes),
            key=lambda c: (c["nombre"] or "").upper(),
        ),
        "operadores": sorted(
            (
                {
                    "id": o["operador_id"],
                    "nombre": f"{o['operador__first_name'] or ''} {o['operador__last_name'] or ''}".strip()
                    or o["operador__email"],
                }
                for o in operadores
            ),
            key=lambda o: (o["nombre"] or "").upper(),
        ),
    }


def contexto_filas(pickings):
    """Lo que cada fila necesita y no está en el picking, para toda la página en
    dos consultas (packings y surtido por programación), no una por fila:

    - ``empaque``: ``{picking_id: [{id, folio, estado, despachado}]}`` de sus
      packings no cancelados.
    - ``surtido``: ``{pedido_id: {destino: piezas}}``.
    """
    from wms.services.picking_pipeline.catalogs import surtido_por_destino_de_pedidos

    pickings = list(pickings)
    empaque = {p.pk: [] for p in pickings}
    if pickings:
        filas = (
            Packing.objects.filter(picking_id__in=empaque.keys())
            .exclude(estado="CANCELADO")
            .annotate(despachado=Exists(Despacho.objects.filter(packing=OuterRef("pk"))))
            .order_by("id")
            .values("id", "picking_id", "folio", "estado", "despachado")
        )
        for f in filas:
            empaque[f.pop("picking_id")].append(f)
    pedido_ids = {p.pedido_id for p in pickings if p.programacion_destino}
    surtido = surtido_por_destino_de_pedidos(pedido_ids) if pedido_ids else {}
    return {"empaque": empaque, "surtido": surtido}


def estado_empaque(packings):
    if not packings:
        return ESTADO_POR_EMPACAR
    return ESTADO_ENVIADO if any(p["despachado"] for p in packings) else ESTADO_EMPACADO


def avance_programacion(picking, surtido_pedido):
    """``{destino, cantidad, surtido}`` del destino al que abona el picking: lo
    programado a ese destino en el pedido y todo lo surtido hacia él."""
    from produccion.services.common import programado_a_destino

    destino = picking.programacion_destino
    if not destino:
        return None
    return {
        "destino": destino,
        "cantidad": programado_a_destino(picking.pedido, destino),
        "surtido": float(surtido_pedido.get(destino, 0)),
    }


def fecha_entrega(picking):
    return rango_fecha_entrega(picking.pedido)[1]


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
