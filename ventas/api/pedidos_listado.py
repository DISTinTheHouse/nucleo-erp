"""Listado de pedidos en modo servidor (EC-782): filtros, columnas anotadas,
``?ordering=``, exportación, KPIs de mesa de control y opciones de filtros.

Lo consumen Mesa de Control (``/operations/orders``), Compras › Pedidos
(``/procurement/orders``) y Mis pedidos (``/sales/orders``), que hoy bajan la
lista completa y calculan todo en el navegador. Replica lo que esas pantallas
hacen en memoria (``OperationsOrderColumns.tsx``, ``ProcurementOrderColumns.tsx``,
``SalesOrderColumns.tsx`` y ``utils/columnFilterOptions.ts`` del front):

- Confirmado = ``fecha_confirmacion`` puesta (EC-748), no el booleano contable
  ``por_confirmar``.
- Rangos (``*_min``/``*_max``): mínimo inclusivo, máximo exclusivo, como
  ``matchesRangeFilter`` del front.
- Muestras: "con" = alguna pieza de muestra, "solo" = todas las piezas son de
  muestra, "sin" = ninguna (``getMuestrasLabel`` del front, sobre ``piezas`` y
  ``piezas_muestra``).
- Piezas programadas = Σ ``programacion_conf.programaciones[].cantidad``: la
  misma suma que valida ``editar-mesa-control`` y que pinta el front; ``programar``
  sólo guarda cantidades enteras ≥ 1.
- Meses en la zona del proyecto (America/Mexico_City), la del navegador de quien
  opera.

Los importes siguen la regla de lectura del pedido (``puede_ver_contabilidad``,
EC-784): a quien no los ve se le ignoran en silencio los filtros de rango y el
orden por importe (mismo criterio que ``OrdenamientoSinMontosFilter`` de OC,
#837) — de lo contrario el número de filas o su orden dejarían deducirlos.

Todo se resuelve en la consulta del listado (subconsultas por FK indexada) o en
una sola consulta agregada: nada por fila.
"""

from decimal import Decimal

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import CharField, Count, DateField, F, Func, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Concat, JSONObject, NullIf, Trim, Trunc
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from nucleo.kpis import entero_param
from nucleo.listados import OrdenamientoEstableFilter, booleano_param
from nucleo.opciones_filtros import clave_orden, mes_param, opciones_meses
from ventas.models import Pedido, PedidoDetalleTalla
from ventas.services.pedido_field_filter_service import puede_ver_contabilidad
from ventas.services.sku_produccion_service import TALLAS_DE_LINEA_NO_MUESTRA_Q
from wms.models import Picking, PickingDetalle

# Acciones que pasan por los filtros del listado en ``PedidoViewSet.get_queryset``.
# Las de detalle no: un filtro suelto en una acción de detalle (``get_object()``
# también pasa por ``get_queryset``) puede dar 404 sobre un pedido visible.
ACCION_KPIS_MESA = "kpis_mesa"
ACCIONES_FILTRADAS = ("list", "exportar", ACCION_KPIS_MESA)
# Las que además ordenan y serializan columnas: llevan todas las anotaciones.
ACCIONES_CON_COLUMNAS = ("list", "exportar")

CAMPOS_IMPORTE = ("subtotal", "gran_total")
# ``?ordering=`` sobre anotaciones de ``anotar_columnas`` (sólo en
# ``ACCIONES_CON_COLUMNAS``); ``vendedor_nombre`` deja los vacíos al final en
# ambos sentidos, como el front.
CAMPOS_ORDEN_ANOTADOS = ("piezas", "piezas_programadas", "piezas_surtidas", "vendedor_nombre")
CAMPOS_ORDEN_NULOS_AL_FINAL = ("vendedor_nombre",)
CAMPOS_ORDEN = (*CAMPOS_ORDEN_ANOTADOS, *CAMPOS_IMPORTE)

MUESTRAS = ("con", "solo", "sin")
SIN_CONFIRMAR = "sin_confirmar"


# --- Anotaciones -----------------------------------------------------------------


class SumaCantidadesProgramadas(Func):
    """Σ ``cantidad`` de ``programacion_conf.programaciones``, 0 sin programación.
    Tolera un ``programacion_conf`` nulo, sin la llave o con algo que no sea
    arreglo (``jsonb_array_elements`` fallaría) y cantidades no numéricas."""

    template = (
        "(SELECT COALESCE(SUM((e->>'cantidad')::numeric), 0)::integer "
        "FROM jsonb_array_elements("
        "CASE WHEN jsonb_typeof(%(expressions)s->'programaciones') = 'array' "
        "THEN %(expressions)s->'programaciones' ELSE '[]'::jsonb END) AS e "
        "WHERE jsonb_typeof(e->'cantidad') = 'number')"
    )
    output_field = IntegerField()


def _suma_tallas(excluir=None):
    qs = PedidoDetalleTalla.objects.filter(pedido_detalle__pedido_id=OuterRef("pk"))
    if excluir is not None:
        qs = qs.exclude(excluir)
    return qs.values("pedido_detalle__pedido_id").annotate(total=Sum("cantidad")).values("total")[:1]


def anotar_piezas(qs):
    """``piezas`` (EC-571, PE-01: todas las tallas, muestras incluidas) y
    ``piezas_muestra`` (tallas de líneas de muestra, mismo criterio que
    ``es_muestra``). Subquery y no ``Sum`` sobre el join: sin ``GROUP BY`` ni
    JOINs multivaluados en la consulta principal, así el filtro y el orden por
    piezas no multiplican filas."""
    return qs.annotate(
        piezas=Coalesce(Subquery(_suma_tallas()), 0),
        piezas_muestra=Coalesce(Subquery(_suma_tallas(TALLAS_DE_LINEA_NO_MUESTRA_Q)), 0),
    )


def anotar_columnas(qs):
    """El resto de columnas del listado y de ``?ordering=``."""
    # ``piezas_surtidas``: Σ ``cantidad_surtida`` de sus pickings, mismo alcance que
    # ``tracker_picking.total_surtido`` (picking y renglón no cancelados, con talla).
    # Sin pickings, 0 (como ya lo pintaba el serializer).
    surtido_qs = (
        PickingDetalle.objects.filter(pedido_detalle__pedido_id=OuterRef("pk"), pedido_detalle_talla__isnull=False)
        .exclude(picking__estado=Picking.Estado.CANCELADO)
        .exclude(estado=PickingDetalle.EstadoLinea.CANCELADA)
        .values("pedido_detalle__pedido_id")
        .annotate(total=Sum("cantidad_surtida"))
        .values("total")[:1]
    )
    return qs.annotate(
        piezas_surtidas=Coalesce(Subquery(surtido_qs), Value(Decimal("0"))),
        piezas_programadas=SumaCantidadesProgramadas(F("programacion_conf")),
        # ``vendedor_nombre`` (la columna que pinta el listado y la de ``?ordering=``)
        # reproduce ``nombre_para_mostrar``: nombre completo o, si queda vacío, el
        # correo; ``None`` sin cotización o sin vendedor. Solo las columnas del
        # nombre, no las filas completas de cotización y usuario.
        vendedor_nombre=Coalesce(
            NullIf(
                Trim(Concat(
                    Coalesce(F("cotizacion__vendedor__first_name"), Value("")),
                    Value(" "),
                    Coalesce(F("cotizacion__vendedor__last_name"), Value("")),
                )),
                Value(""),
            ),
            F("cotizacion__vendedor__email"),
            output_field=CharField(),
        ),
    )


# --- Filtros ---------------------------------------------------------------------


def _texto(request, nombre):
    return (request.query_params.get(nombre) or "").strip()


def _entero_piezas(request, nombre, campo=None):
    """``piezas_min``/``piezas_max`` (``entero_param``: vacío → ``None``, una fracción o
    texto → 400); ``piezas`` es entero y truncar ``0.5`` a ``0`` cambiaba el rango.
    Único lector con signo (EC-829): un mínimo negativo es válido desde EC-782
    (no excluye ningún pedido)."""
    return entero_param(request, nombre, con_signo=True)


def _importe(request, nombre, campo):
    """Importe del query string con la precisión de ``Pedido.{campo}``
    (``max_digits``/``decimal_places``, el mismo ``DecimalField`` de DRF que valida la
    escritura); vacío → ``None``. Fuera de rango → 400: sin esto, un valor que la
    columna no puede representar llegaba a Postgres y respondía 500."""
    valor = _texto(request, nombre)
    if not valor:
        return None
    modelo = Pedido._meta.get_field(campo)
    validador = serializers.DecimalField(max_digits=modelo.max_digits, decimal_places=modelo.decimal_places)
    try:
        return validador.run_validation(valor)
    except ValidationError as error:
        raise ValidationError({nombre: error.detail})


def _rango(request, qs, campo, lector):
    """``?{campo}_min=`` (inclusivo) y ``?{campo}_max=`` (exclusivo); ``lector(request,
    nombre, campo)`` lee y valida cada uno."""
    minimo = lector(request, f"{campo}_min", campo)
    maximo = lector(request, f"{campo}_max", campo)
    if minimo is not None:
        qs = qs.filter(**{f"{campo}__gte": minimo})
    if maximo is not None:
        qs = qs.filter(**{f"{campo}__lt": maximo})
    return qs


def filtra_piezas(request):
    """``True`` si la petición filtra por piezas o muestras (necesita
    ``anotar_piezas`` aunque la acción no pinte columnas)."""
    return any(_texto(request, n) for n in ("piezas_min", "piezas_max", "muestras"))


def filtrar(request, qs, ver_importes):
    """Filtros de columna sobre campos de ``pedidos`` (sin anotaciones). Valores
    fuera de catálogo → 400. Los rangos de importe se ignoran sin permiso."""
    moneda = entero_param(request, "moneda")
    if moneda is not None:
        qs = qs.filter(moneda_id=moneda)

    clasificacion = _texto(request, "clasificacion")
    if clasificacion:
        codigos = [c.strip().upper() for c in clasificacion.split(",") if c.strip()]
        invalidos = sorted(set(codigos) - set(Pedido.Clasificacion.values))
        if invalidos or not codigos:
            raise ValidationError({"clasificacion": f"Usa uno o varios de: {', '.join(Pedido.Clasificacion.values)}."})
        qs = qs.filter(clasificacion__in=codigos)

    confirmado = booleano_param(request, "confirmado", None)
    if confirmado is not None:
        qs = qs.filter(fecha_confirmacion__isnull=not confirmado)

    mes = mes_param(request, "mes")
    if mes:
        qs = qs.filter(created_at__year=mes[0], created_at__month=mes[1])

    mes_confirmacion = mes_param(request, "mes_confirmacion", permitir=(SIN_CONFIRMAR,))
    if mes_confirmacion == SIN_CONFIRMAR:
        qs = qs.filter(fecha_confirmacion__isnull=True)
    elif mes_confirmacion:
        qs = qs.filter(
            fecha_confirmacion__year=mes_confirmacion[0], fecha_confirmacion__month=mes_confirmacion[1],
        )

    muestras = _texto(request, "muestras")
    if muestras and muestras not in MUESTRAS:
        raise ValidationError({"muestras": f"Usa uno de: {', '.join(MUESTRAS)}."})

    if ver_importes:
        for campo in CAMPOS_IMPORTE:
            qs = _rango(request, qs, campo, _importe)
    return qs


def filtrar_piezas(request, qs):
    """Rangos de ``piezas`` y ``?muestras=``; requiere ``anotar_piezas``."""
    qs = _rango(request, qs, "piezas", _entero_piezas)
    muestras = _texto(request, "muestras")
    if muestras == "con":
        qs = qs.filter(piezas_muestra__gt=0)
    elif muestras == "solo":
        qs = qs.filter(piezas_muestra__gt=0, piezas_muestra__gte=F("piezas"))
    elif muestras == "sin":
        qs = qs.filter(piezas_muestra__lte=0)
    return qs


# --- Ordenamiento ----------------------------------------------------------------


class OrdenamientoPedidosFilter(OrdenamientoEstableFilter):
    """``?ordering=`` del listado de pedidos: la lista blanca de la vista, más las
    columnas anotadas sólo donde existen (``list``/``exportar``; en el resto de
    acciones se descartan, como cualquier campo inválido) y sin importes para
    quien no los ve (#837, EC-784). ``vendedor_nombre`` deja los vacíos al final."""

    def remove_invalid_fields(self, queryset, fields, view, request):
        validos = super().remove_invalid_fields(queryset, fields, view, request)
        if getattr(view, "action", None) not in ACCIONES_CON_COLUMNAS:
            validos = [f for f in validos if f.lstrip("-") not in CAMPOS_ORDEN_ANOTADOS]
        if not puede_ver_contabilidad(request.user):
            validos = [f for f in validos if f.lstrip("-") not in CAMPOS_IMPORTE]
        return validos

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset
        return queryset.order_by(*[_orden(campo) for campo in ordering])


def _orden(campo):
    nombre = campo.lstrip("-")
    if nombre not in CAMPOS_ORDEN_NULOS_AL_FINAL:
        return campo
    return F(nombre).desc(nulls_last=True) if campo.startswith("-") else F(nombre).asc(nulls_last=True)


# --- Exportación -----------------------------------------------------------------

COLUMNAS_EXPORTACION = (
    "id", "folio", "estatus", "oc", "cliente", "cliente_razon_social", "cliente_nombre",
    "created_at", "fecha_confirmacion", "clasificacion", "piezas", "vendedor_nombre",
    "subtotal", "gran_total", "moneda",
)


# Filas máximas de ``exportar/`` (#971): por encima, 400 pidiendo acotar los filtros
# en vez de un archivo recortado sin aviso. Holgado frente al volumen actual (~170).
TOPE_EXPORTACION = 5000


class ExportacionExcedeTope(Exception):
    pass


def filas_exportacion(qs, ver_importes):
    """Las filas filtradas y ordenadas, sin paginar, en UNA consulta (``.values()``,
    sin instanciar pedidos ni serializer). Pide ``TOPE_EXPORTACION + 1`` filas: si
    llegan más del tope lanza ``ExportacionExcedeTope``, sin un ``count`` aparte."""
    columnas = [c for c in COLUMNAS_EXPORTACION if ver_importes or c not in CAMPOS_IMPORTE]
    etiquetas = dict(Pedido.CHOICES_ESTATUS)
    filas = []
    valores = qs.values(*columnas, moneda_codigo=F("moneda__codigo_iso"))[: TOPE_EXPORTACION + 1]
    for fila in valores:
        if len(filas) == TOPE_EXPORTACION:
            raise ExportacionExcedeTope()
        fila["estatus_display"] = etiquetas.get(fila["estatus"], str(fila["estatus"]))
        filas.append(fila)
    return filas


# --- KPIs de mesa de control ------------------------------------------------------


def kpis_mesa(qs, ver_importes):
    """Conteos y valor por moneda del conjunto filtrado, en UNA consulta agrupada por
    moneda. ``total``, ``confirmados``, ``por_confirmar`` y ``por_moneda`` excluyen
    los cancelados (``estatus`` 5): ``total`` = ``confirmados`` + ``por_confirmar``.
    ``cancelados`` los cuenta aparte, así que ``total`` + ``cancelados`` = filas del
    listado con los mismos filtros. Los cancelados con ``POST /cancelar/`` quedan
    además ``activo=False`` y no están en ninguno de los dos (``pedidos_base``)."""
    vigente = ~Q(estatus=Pedido.ESTATUS_CANCELADO)
    filas = list(
        qs.order_by()
        .values("moneda_id", "moneda__codigo_iso")
        .annotate(
            total=Count("id", filter=vigente),
            confirmados=Count("id", filter=vigente & Q(fecha_confirmacion__isnull=False)),
            cancelados=Count("id", filter=~vigente),
            valor=Sum("gran_total", filter=vigente),
        )
        .order_by("moneda__codigo_iso", "moneda_id")
    )
    confirmados = sum(f["confirmados"] for f in filas)
    total = sum(f["total"] for f in filas)
    por_moneda = []
    for f in filas:
        if not f["total"]:
            continue  # moneda con solo cancelados: no aporta valor ni pedidos vigentes
        fila = {"moneda_id": f["moneda_id"], "moneda": f["moneda__codigo_iso"], "total": f["total"]}
        if ver_importes:
            fila["valor"] = f["valor"] or Decimal("0")
        por_moneda.append(fila)
    return {
        "total": total,
        "confirmados": confirmados,
        "por_confirmar": total - confirmados,
        "cancelados": sum(f["cancelados"] for f in filas),
        "por_moneda": por_moneda,
        "puede_ver_importes": ver_importes,
    }


# --- Opciones de filtros -----------------------------------------------------------


def opciones(qs):
    """Opciones de los filtros de encabezado sobre el conjunto visible completo (no
    reaccionan a filtros, como ``por-almacen/resumen/`` de EC-774), en UNA consulta.
    Los nombres de cliente y vendedor salen de su catálogo (una etiqueta por id),
    no del snapshot de cada pedido."""
    datos = qs.order_by().aggregate(
        clientes=ArrayAgg(
            JSONObject(
                value=F("cliente_id"),
                label=Coalesce(NullIf(Trim(F("cliente__razon_social")), Value("")), F("cliente__nombre")),
            ),
            distinct=True,
        ),
        vendedores=ArrayAgg(
            JSONObject(
                value=F("cotizacion__vendedor_id"),
                first_name=F("cotizacion__vendedor__first_name"),
                last_name=F("cotizacion__vendedor__last_name"),
                email=F("cotizacion__vendedor__email"),
            ),
            distinct=True,
            filter=Q(cotizacion__vendedor__isnull=False),
        ),
        meses=ArrayAgg(
            Trunc("created_at", "month", output_field=DateField()),
            distinct=True,
            filter=Q(created_at__isnull=False),
        ),
        meses_confirmacion=ArrayAgg(
            Trunc("fecha_confirmacion", "month", output_field=DateField()),
            distinct=True,
            filter=Q(fecha_confirmacion__isnull=False),
        ),
        sin_confirmar=Count("id", filter=Q(fecha_confirmacion__isnull=True)),
        clasificaciones=ArrayAgg("clasificacion", distinct=True, filter=Q(clasificacion__isnull=False)),
    )
    from ventas.utils.helpers import nombre_para_mostrar

    clientes = sorted(
        ({"value": c["value"], "label": (c["label"] or "").strip()} for c in datos["clientes"] or []),
        key=lambda o: clave_orden(o["label"]),
    )
    vendedores = sorted(
        (
            {"value": v["value"], "label": nombre_para_mostrar(v["first_name"], v["last_name"], v["email"])}
            for v in datos["vendedores"] or []
        ),
        key=lambda o: clave_orden(o["label"] or ""),
    )
    etiquetas = dict(Pedido.Clasificacion.choices)
    meses_confirmacion = opciones_meses(datos["meses_confirmacion"])
    if datos["sin_confirmar"]:
        meses_confirmacion.append({"value": SIN_CONFIRMAR, "label": "Sin confirmar"})
    return {
        "cliente": clientes,
        "vendedor": vendedores,
        "mes": opciones_meses(datos["meses"]),
        "mes_confirmacion": meses_confirmacion,
        "clasificacion": [
            {"value": c, "label": etiquetas.get(c, c)} for c in sorted(datos["clasificaciones"] or [])
        ],
    }
