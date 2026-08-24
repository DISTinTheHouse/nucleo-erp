from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum

from ventas.models import PedidoDetalleTalla
from wms.models import Picking, PickingDetalle
from wms.services.existencia_service import ExistenciaService
from wms.utils.decimales import normalizar_decimal


D = Decimal
_PCT_Q = D("0.0001")


def _safe_pct(num, den):
    try:
        n = D(num)
        d = D(den)
        if d <= 0:
            return D("0")
        r = (n / d).quantize(_PCT_Q, rounding=ROUND_HALF_UP)
        if r < 0:
            return D("0")
        if r > D("1"):
            return D("1")
        return r
    except Exception:
        return D("0")


def _picking_scope_queryset(pedido):
    """Scope base de ``PickingDetalle`` (pickings activos y no cancelados)."""
    return PickingDetalle.objects.filter(
        pedido_detalle__pedido=pedido,
        pedido_detalle_talla__isnull=False,
    ).exclude(
        picking__estado=Picking.Estado.CANCELADO,
    ).exclude(
        estado=PickingDetalle.EstadoLinea.CANCELADA,
    )


def historical_maps(pedido, talla_ids=None):
    """Cantidad asignada y surtida históricamente por ``PedidoDetalleTalla``.

    Devuelve dos ``defaultdict(Decimal("0"))`` keyed por ``talla.id``.

    - ``asignado_map``: suma de ``cantidad_asignada`` de pickings activos
      (surtido + en curso).
    - ``surtido_map``: suma de ``cantidad_surtida`` (lo realmente entregado).
    """
    qs = _picking_scope_queryset(pedido)
    if talla_ids is not None:
        qs = qs.filter(pedido_detalle_talla_id__in=talla_ids)

    asignado_map = defaultdict(lambda: Decimal("0"))
    surtido_map = defaultdict(lambda: Decimal("0"))
    for row in qs.values("pedido_detalle_talla_id").annotate(
        total_asignado=Sum("cantidad_asignada"),
        total_surtido=Sum("cantidad_surtida"),
    ):
        talla_id = row["pedido_detalle_talla_id"]
        asignado_map[talla_id] = normalizar_decimal(row["total_asignado"])
        surtido_map[talla_id] = normalizar_decimal(row["total_surtido"])
    return asignado_map, surtido_map


@dataclass(slots=True)
class PickingLineaSnapshot:
    """Snapshop consistente de una ``PedidoDetalleTalla`` para GET y POST.

    La idea es que el calculo de (pedida, asignada, surtida, pendiente,
    existencia, maximo_picking_permitido) se haga **una sola vez** y lo
    consuman tanto el ``onboarding_payload`` (sugerir cantidades al UI)
    como el ``_resolve_requested_items`` (validar lo que envía el UI).

    Antes estas dos rutas reimplementaban el mismo bloque línea a línea,
    con riesgo de divergencia (ej: GET aceptaba Decimal("0") negativo pero
    POST no).
    """

    talla: PedidoDetalleTalla
    cantidad_pedida: Decimal
    cantidad_asignada_historica: Decimal
    cantidad_surtida_historica: Decimal
    cantidad_pendiente: Decimal
    existencia_fisica: Decimal = Decimal("0")
    existencia_reservada: Decimal = Decimal("0")
    existencia_disponible: Decimal = Decimal("0")

    @property
    def maximo_picking_permitido(self):
        m = min(self.cantidad_pendiente, self.existencia_disponible)
        return m if m >= Decimal("0") else Decimal("0")

    @property
    def clave_stock(self):
        return (
            getattr(self.talla.pedido_detalle, "producto_id", None),
            getattr(self.talla, "variante_id", None),
        )


def build_snapshots(tallas, pedido, almacen_origen=None):
    """Construye ``PickingLineaSnapshot``s para las tallas de un pedido.

    - Si ``almacen_origen`` está presente, consulta existencia en batch
      (una sola consulta agregada por la clave de stock, no N+1).
    - Si falta ``almacen_origen``, las cantidades de existencia quedan en
      ``Decimal("0")`` pero el resto de campos (pendiente, histórico) sí
      se calculan (útil para el preview del onboarding sin almacén).
    """
    asignado_map, surtido_map = historical_maps(
        pedido, talla_ids=[t.id for t in tallas]
    )

    existencia_by_talla = {}
    if almacen_origen:
        existencia_by_talla = ExistenciaService.get_existencia_batch(
            almacen_origen, tallas
        )

    snapshots = []
    for talla in tallas:
        cantidad_pedida = normalizar_decimal(talla.cantidad)
        cantidad_asignada = asignado_map[talla.id]
        cantidad_surtida = surtido_map[talla.id]
        cantidad_pendiente = cantidad_pedida - cantidad_asignada
        if cantidad_pendiente < Decimal("0"):
            cantidad_pendiente = Decimal("0")

        existencia_row = existencia_by_talla.get(
            talla.id,
            {"fisica": Decimal("0"), "reservada": Decimal("0"), "disponible": Decimal("0")},
        )

        snapshots.append(
            PickingLineaSnapshot(
                talla=talla,
                cantidad_pedida=cantidad_pedida,
                cantidad_asignada_historica=cantidad_asignada,
                cantidad_surtida_historica=cantidad_surtida,
                cantidad_pendiente=cantidad_pendiente,
                existencia_fisica=normalizar_decimal(existencia_row.get("fisica")),
                existencia_reservada=normalizar_decimal(existencia_row.get("reservada")),
                existencia_disponible=normalizar_decimal(existencia_row.get("disponible")),
            )
        )
    return snapshots


# ---------------------------------------------------------------------------
# Payloads de ``tracker_picking`` para detalle de Pedido / Línea / Talla
# ---------------------------------------------------------------------------
# SSoT: no filtra por almacén destino — suma TODOS los Picking activos del
# pedido sin importar a qué almacén fueron enviados (regla de negocio del
# tracker: surtido = lo que YA tiene folio de picking, independientemente
# de su ubicación física actual).
# ---------------------------------------------------------------------------

def armar_tracker_pedido(pedido):
    """Devuelve los 5 KPIs de surtido por Picking activo a nivel PEDIDO.

    Shape exacto que ``PickingService._armar_tracker`` (SSoT) para que el
    onboarding y el detalle del pedido entreguen exactamente la misma
    métrica y no haya desalineación en UI:
        {pct_asignado_pedido, pct_surtido_pedido,
         total_prendas_pedido, total_asignado, total_surtido}
    Todos los números son ``str`` Decimal normalizados.
    """
    if pedido is None:
        return {
            "pct_asignado_pedido": "0.0000",
            "pct_surtido_pedido": "0.0000",
            "total_prendas_pedido": "0",
            "total_asignado": "0",
            "total_surtido": "0",
        }
    total_pedido_qs = PedidoDetalleTalla.objects.filter(
        pedido_detalle__pedido=pedido
    ).aggregate(total=Sum("cantidad"))
    total_pedido = normalizar_decimal(total_pedido_qs["total"] or D("0"))
    asignado_map, surtido_map = historical_maps(pedido)
    total_asignado = normalizar_decimal(sum(asignado_map.values(), D("0")))
    total_surtido = normalizar_decimal(sum(surtido_map.values(), D("0")))
    pct_asignado = _safe_pct(total_asignado, total_pedido) * D("100")
    pct_surtido = _safe_pct(total_surtido, total_pedido) * D("100")
    return {
        "pct_asignado_pedido": str(pct_asignado.quantize(_PCT_Q, rounding=ROUND_HALF_UP)),
        "pct_surtido_pedido": str(pct_surtido.quantize(_PCT_Q, rounding=ROUND_HALF_UP)),
        "total_prendas_pedido": str(total_pedido),
        "total_asignado": str(total_asignado),
        "total_surtido": str(total_surtido),
    }


def armar_tracker_linea(pedido_detalle, asignado_map, surtido_map):
    """Tracker de surtido a nivel PEDIDO-DETALLE (línea del pedido).

    Mismas 5 keys pero a nivel de la línea:
      - total_prendas_linea: suma cantidades de las tallas del renglón
      - total_asignado_linea / total_surtido_linea: sumatoria por PDT de la línea
      - pct_asignado_linea / pct_surtido_linea: normalizados 4 decimales (0.0000 - 100.0000)

    Recibe ``asignado_map`` y ``surtido_map`` YA cargados (llamados **una sola vez**
    por el pedido completo desde el serializer, para evitar N queries por línea).
    """
    total_linea = D("0")
    asig_linea = D("0")
    surt_linea = D("0")
    # ``pedido_detalle.pedidodetalletalla_set`` viene prefetcheado por
    # ``_pedido_detalles_prefetch`` en el viewset, así que aquí hay 0 queries.
    for talla in pedido_detalle.pedidodetalletalla_set.all():
        t_cant = normalizar_decimal(talla.cantidad)
        total_linea += t_cant
        asig_linea += asignado_map.get(talla.id, D("0"))
        surt_linea += surtido_map.get(talla.id, D("0"))
    pct_a = _safe_pct(asig_linea, total_linea) * D("100")
    pct_s = _safe_pct(surt_linea, total_linea) * D("100")
    return {
        "pct_asignado_linea": str(pct_a.quantize(_PCT_Q, rounding=ROUND_HALF_UP)),
        "pct_surtido_linea": str(pct_s.quantize(_PCT_Q, rounding=ROUND_HALF_UP)),
        "total_prendas_linea": str(normalizar_decimal(total_linea)),
        "total_asignado_linea": str(normalizar_decimal(asig_linea)),
        "total_surtido_linea": str(normalizar_decimal(surt_linea)),
    }


def listar_folios_picking(pedido):
    """Lista compacta de folios de picking activos del pedido (trazabilidad almacén).

    Regresa los pickings asociados, SIN importar el almacén destino (por
    definición del tracker). Útil para UI: cada item dice a qué almacén se
    mandaron esas prendas y cuántas, para poder abrir el detalle del picking
    y ver el historial completo.
    """
    if pedido is None:
        return []
    detalle_qs = (
        _picking_scope_queryset(pedido)
        .select_related(
            "picking",
            "picking__almacen",
            "picking__almacen_destino",
            "picking__operador",
        )
    )
    # Agregamos a nivel Picking id en memoria usando los prefetched
    # (mismo número de rows que el qs de detalle).
    by_picking = defaultdict(lambda: {"cantidad_asignada": D("0"), "cantidad_surtida": D("0")})
    for row in detalle_qs:
        by_picking[row.picking_id]["cantidad_asignada"] += normalizar_decimal(
            row.cantidad_asignada or 0
        )
        by_picking[row.picking_id]["cantidad_surtida"] += normalizar_decimal(
            row.cantidad_surtida or 0
        )

    picks_qs = (
        Picking.objects.filter(id__in=list(by_picking.keys()))
        .select_related("almacen", "almacen_destino", "operador")
        .order_by("-id")
    )
    out = []
    for pk in picks_qs:
        agg = by_picking[pk.id]
        out.append({
            "id": pk.id,
            "folio": pk.folio,
            "estado": pk.estado,
            "created_at": pk.created_at,
            "almacen_origen": pk.almacen_id,
            "almacen_origen_nombre": getattr(pk.almacen, "nombre", None),
            "almacen_destino": pk.almacen_destino_id,
            "almacen_destino_nombre": getattr(pk.almacen_destino, "nombre", None),
            "operador": pk.operador_id,
            "operador_nombre": (
                pk.operador.get_full_name().strip()
                if getattr(pk.operador, "get_full_name", None)
                else (getattr(pk.operador, "email", None) or None)
            ),
            "total_lineas": pk.total_lineas,
            "total_lineas_completas": pk.total_lineas_completas,
            "cantidad_asignada_total": str(normalizar_decimal(agg["cantidad_asignada"])),
            "cantidad_surtida_total": str(normalizar_decimal(agg["cantidad_surtida"])),
        })
    return out
