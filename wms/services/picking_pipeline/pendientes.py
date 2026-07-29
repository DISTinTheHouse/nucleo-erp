from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

from ventas.models import PedidoDetalleTalla
from wms.models import Picking, PickingDetalle
from wms.services.existencia_service import ExistenciaService
from wms.utils.decimales import normalizar_decimal


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
        return m if m > Decimal("0") else Decimal("0")

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
