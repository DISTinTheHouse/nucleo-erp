from collections import defaultdict
from decimal import Decimal

from rest_framework.exceptions import ValidationError

from ventas.models import PedidoDetalleTalla
from wms.services.existencia_service import ExistenciaService
from wms.services.picking_pipeline.pendientes import build_snapshots
from wms.utils.decimales import normalizar_decimal


def _agrupar_requested_rows(requested_rows):
    """Descompone el input del POST en maps keyed por ``talla_id``.

    Devuelve:
    - ``quantity_by_talla``: suma de ``cantidad_asignada`` por talla
      (permitimos que el frontend envíe la misma talla en >1 renglón,
      aunque el serializer actual lo impida).
    - ``extras_by_talla``: flags (generar_orden_*) + observaciones.
    """
    quantity_by_talla = defaultdict(lambda: Decimal("0"))
    extras_by_talla = {}

    for row in requested_rows:
        talla_id = row.get("pedido_detalle_talla")
        if not talla_id:
            raise ValidationError(
                {"picking_detalle": "Cada línea debe incluir pedido_detalle_talla."}
            )
        cantidad = normalizar_decimal(row.get("cantidad_asignada"))
        if cantidad <= 0:
            raise ValidationError(
                {
                    "picking_detalle": (
                        "Cada línea debe incluir una cantidad_asignada mayor a cero."
                    )
                }
            )
        key = int(talla_id)
        quantity_by_talla[key] += cantidad
        extras_by_talla.setdefault(
            key,
            {
                "generar_orden_bordado": bool(row.get("generar_orden_bordado", False)),
                "generar_orden_reflejante": bool(
                    row.get("generar_orden_reflejante", False)
                ),
                "generar_orden_corte_manga": bool(
                    row.get("generar_orden_corte_manga", False)
                ),
                "observaciones": row.get("observaciones") or "",
            },
        )
    return quantity_by_talla, extras_by_talla


def _cargar_tallas_del_pedido(pedido, talla_ids):
    tallas = list(
        PedidoDetalleTalla.objects.filter(
            pedido_detalle__pedido=pedido,
            pk__in=list(talla_ids),
        )
        .select_related(
            "pedido_detalle__producto",
            "variante",
            "variante__talla",
            "variante__color",
        )
        .order_by("pedido_detalle_id", "id")
    )
    if len(tallas) != len(talla_ids):
        raise ValidationError(
            {"picking_detalle": "Una o más líneas no pertenecen al pedido indicado."}
        )
    return tallas


def _validar_linea_por_linea(snapshots_by_talla, quantity_by_talla, extras_by_talla, almacen_origen):
    requested_items = []
    for talla_id, cantidad_solicitada in quantity_by_talla.items():
        snap = snapshots_by_talla[talla_id]
        talla = snap.talla
        if snap.cantidad_pendiente <= Decimal("0"):
            raise ValidationError(
                {
                    "picking_detalle": (
                        f"La línea de talla {talla.id} ya no tiene cantidad pendiente."
                    )
                }
            )
        if cantidad_solicitada > snap.cantidad_pendiente:
            raise ValidationError(
                {
                    "picking_detalle": (
                        f"La cantidad solicitada para la talla {talla.id} excede lo pendiente "
                        f"(solicitada={cantidad_solicitada}, "
                        f"pendiente={snap.cantidad_pendiente})."
                    )
                }
            )
        if almacen_origen and cantidad_solicitada > snap.existencia_disponible:
            raise ValidationError(
                {
                    "picking_detalle": (
                        f"La cantidad solicitada para la talla {talla.id} excede la existencia "
                        f"disponible en el almacén origen "
                        f"(solicitada={cantidad_solicitada}, "
                        f"disponible={snap.existencia_disponible}, "
                        f"pendiente_pedido={snap.cantidad_pendiente})."
                    )
                }
            )

        extras = extras_by_talla.get(talla.id, {})
        for flag, attr, label in (
            ("generar_orden_bordado", "lleva_bordado", "bordado"),
            ("generar_orden_reflejante", "lleva_reflejante", "reflejante"),
            ("generar_orden_corte_manga", "lleva_corte_manga", "corte de manga"),
        ):
            if extras.get(flag) and not getattr(talla, attr, False):
                raise ValidationError(
                    {
                        "picking_detalle": (
                            f"La línea de talla {talla.id} no requiere {label}; "
                            f"no puede marcar {flag}."
                        )
                    }
                )

        requested_items.append(
            {
                "talla": talla,
                "cantidad": cantidad_solicitada,
                "cantidad_pedida": snap.cantidad_pedida,
                "cantidad_asignada_historica": snap.cantidad_asignada_historica,
                "cantidad_surtida_historica": snap.cantidad_surtida_historica,
                "cantidad_pendiente": snap.cantidad_pendiente,
                "existencia_disponible": snap.existencia_disponible,
                "generar_orden_bordado": extras.get("generar_orden_bordado", False),
                "generar_orden_reflejante": extras.get("generar_orden_reflejante", False),
                "generar_orden_corte_manga": extras.get(
                    "generar_orden_corte_manga", False
                ),
                "observaciones": extras.get("observaciones") or "",
                "_clave_stock": snap.clave_stock,
            }
        )
    return requested_items


def _validar_por_clave_stock(requested_items, almacen_origen):
    """Validación agregada por clave ``(producto_id, variante_id)``.

    Previene el colapso F: varias tallas con ``variante=None`` del mismo
    producto no pueden colectivamente agotar la existencia si el frontend
    envía cada una individualmente dentro del ``maximo_picking_permitido``
    de su línea.
    """
    if not almacen_origen:
        return

    solicitado_por_clave = defaultdict(lambda: Decimal("0"))
    disponible_por_clave = {}
    for item in requested_items:
        clave = item["_clave_stock"]
        solicitado_por_clave[clave] += item["cantidad"]
        if clave not in disponible_por_clave:
            _, _, dis = ExistenciaService.get_existencia_agregada(
                almacen=almacen_origen,
                producto=clave[0],
                producto_variante=clave[1],
            )
            disponible_por_clave[clave] = normalizar_decimal(dis)

    for clave, total_solicitado in solicitado_por_clave.items():
        total_disponible = disponible_por_clave.get(clave, Decimal("0"))
        if total_solicitado > total_disponible:
            producto_id, variante_id = clave
            scope = (
                f"producto_variante {variante_id}"
                if variante_id
                else f"producto {producto_id}"
            )
            raise ValidationError(
                {
                    "picking_detalle": (
                        f"La suma de cantidades para el {scope} ({total_solicitado}) "
                        f"excede la existencia disponible agregada ({total_disponible}). "
                        f"Varias líneas del pedido comparten el mismo stock."
                    )
                }
            )


def resolve_requested_items(pedido, requested_rows, almacen_origen=None):
    """Validación total del ``picking_detalle`` de un POST.

    Flow:
      1. Desglosa y normaliza cantidades/extras.
      2. Carga las tallas del pedido (valida que todas pertenezcan).
      3. Construye ``PickingLineaSnapshot`` con histórico y existencias
         (mismo calculo que el GET onboarding).
      4. Valida línea por línea (pendiente, existencia por línea, flags OT).
      5. Valida agregado por clave de stock (colapso variante=None).
    """
    if not requested_rows:
        raise ValidationError(
            {"picking_detalle": "Debe enviar al menos una línea para surtir."}
        )

    quantity_by_talla, extras_by_talla = _agrupar_requested_rows(requested_rows)
    tallas = _cargar_tallas_del_pedido(pedido, quantity_by_talla.keys())
    snapshots = build_snapshots(tallas, pedido, almacen_origen=almacen_origen)
    snapshots_by_talla = {s.talla.id: s for s in snapshots}

    requested_items = _validar_linea_por_linea(
        snapshots_by_talla=snapshots_by_talla,
        quantity_by_talla=quantity_by_talla,
        extras_by_talla=extras_by_talla,
        almacen_origen=almacen_origen,
    )
    _validar_por_clave_stock(requested_items, almacen_origen)
    return requested_items
