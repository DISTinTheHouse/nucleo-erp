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

from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Coalesce, JSONObject, NullIf, Trim
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from nucleo.kpis import entero_param, rango_fechas
from nucleo.listados import estatus_param
from nucleo.opciones_filtros import clave_orden
from ventas.models import Cotizacion

# Acciones que pasan por los filtros del listado en ``CotizacionViewSet._apply_filters``.
# Las de detalle no: un filtro suelto en una acción de detalle (``get_object()``
# también pasa por ``get_queryset``) puede dar 404 sobre una cotización visible.
ACCIONES_FILTRADAS = ("list",)

ESTATUS_VALIDOS = frozenset(e for e, _ in Cotizacion.CHOICES_ESTATUS)
SIN_CLIENTE = "sin_cliente"
ETIQUETA_SIN_CLIENTE = "Sin cliente"


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
