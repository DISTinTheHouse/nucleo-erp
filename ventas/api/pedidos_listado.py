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

import re
from decimal import Decimal, InvalidOperation

from django.db.models import F, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework.exceptions import ValidationError

from nucleo.kpis import entero_param
from nucleo.listados import booleano_param
from ventas.models import Pedido, PedidoDetalleTalla
from ventas.services.sku_produccion_service import TALLAS_DE_LINEA_NO_MUESTRA_Q
from wms.models import Picking, PickingDetalle

# Acciones que pasan por los filtros del listado en ``PedidoViewSet.get_queryset``.
# Las de detalle no: un filtro suelto en una acción de detalle (``get_object()``
# también pasa por ``get_queryset``) puede dar 404 sobre un pedido visible.
ACCIONES_FILTRADAS = ("list",)
# Las que además ordenan y serializan columnas: llevan todas las anotaciones.
ACCIONES_CON_COLUMNAS = ("list",)

CAMPOS_IMPORTE = ("subtotal", "gran_total")
MUESTRAS = ("con", "solo", "sin")
SIN_CONFIRMAR = "sin_confirmar"

_MES_RE = re.compile(r"^(\d{4})-(\d{2})$")


# --- Anotaciones -----------------------------------------------------------------


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
    """El resto de columnas del listado."""
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
    # Solo las columnas del nombre, no las filas completas de cotización y usuario
    # (``aprobado_snapshot`` es un JSONB).
    return qs.annotate(
        piezas_surtidas=Coalesce(Subquery(surtido_qs), Value(Decimal("0"))),
        vendedor_cotizacion_id=F("cotizacion__vendedor_id"),
        vendedor_first_name=F("cotizacion__vendedor__first_name"),
        vendedor_last_name=F("cotizacion__vendedor__last_name"),
        vendedor_email=F("cotizacion__vendedor__email"),
    )


# --- Filtros ---------------------------------------------------------------------


def _texto(request, nombre):
    return (request.query_params.get(nombre) or "").strip()


def _mes(request, nombre, permitir=()):
    """``(año, mes)`` de un ``AAAA-MM``; vacío → ``None``; un valor de ``permitir``
    se devuelve tal cual; otro formato → 400."""
    valor = _texto(request, nombre)
    if not valor:
        return None
    if valor in permitir:
        return valor
    coincide = _MES_RE.match(valor)
    if not coincide or not 1 <= int(coincide.group(2)) <= 12:
        extra = f" o {', '.join(permitir)}" if permitir else ""
        raise ValidationError({nombre: f"Formato AAAA-MM{extra}."})
    return int(coincide.group(1)), int(coincide.group(2))


def _numero(request, nombre):
    """Número (entero o decimal) del query string; vacío → ``None``; inválido → 400."""
    valor = _texto(request, nombre)
    if not valor:
        return None
    try:
        numero = Decimal(valor)
    except InvalidOperation:
        numero = None
    if numero is None or not numero.is_finite():
        raise ValidationError({nombre: "Debe ser un número."})
    return numero


def _rango(request, qs, campo):
    """``?{campo}_min=`` (inclusivo) y ``?{campo}_max=`` (exclusivo)."""
    minimo = _numero(request, f"{campo}_min")
    maximo = _numero(request, f"{campo}_max")
    if minimo is not None:
        qs = qs.filter(**{f"{campo}__gte": minimo})
    if maximo is not None:
        qs = qs.filter(**{f"{campo}__lt": maximo})
    return qs


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

    mes = _mes(request, "mes")
    if mes:
        qs = qs.filter(created_at__year=mes[0], created_at__month=mes[1])

    mes_confirmacion = _mes(request, "mes_confirmacion", permitir=(SIN_CONFIRMAR,))
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
            qs = _rango(request, qs, campo)
    return qs


def filtrar_piezas(request, qs):
    """Rangos de ``piezas`` y ``?muestras=``; requiere ``anotar_piezas``."""
    qs = _rango(request, qs, "piezas")
    muestras = _texto(request, "muestras")
    if muestras == "con":
        qs = qs.filter(piezas_muestra__gt=0)
    elif muestras == "solo":
        qs = qs.filter(piezas_muestra__gt=0, piezas_muestra__gte=F("piezas"))
    elif muestras == "sin":
        qs = qs.filter(piezas_muestra__lte=0)
    return qs


