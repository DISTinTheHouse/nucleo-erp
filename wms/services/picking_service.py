from decimal import Decimal

from django.db import transaction

from rest_framework.exceptions import ValidationError

from ventas.models import PedidoDetalleTalla
from wms.models import Picking, PickingDetalle
from wms.services.existencia_service import ExistenciaService
from wms.services.picking_pipeline.catalogs import (
    armar_header_preview,
    armar_payload_vacio,
    cargar_catalogos,
    resolver_apartados_obligatorio,
    serializar_almacen,
    sugerir_almacenes,
)
from wms.services.picking_pipeline.cantidad_validator import resolve_requested_items
from wms.services.picking_pipeline.context import (
    parse_pk,
    validar_contexto_picking,
)
from wms.services.picking_pipeline.pendientes import build_snapshots
from wms.utils.folios import generate_folio


class PickingService:
    """Facade público del módulo de picking (WMS).

    Modelo **tradicional** de picking:
    - ``onboarding_payload``: catálogos + sugerencias + existencia
      para que el usuario arma el documento.
    - ``handle_store``: **solo crea el documento** ``Picking`` +
      ``PickingDetalle`` + folio.

    Flujo real operativo:
      1. ``GET onboarding`` → seleccionar pedido / origen / destino / cantidades.
      2. ``POST create`` → documento picking listo para operar.
      3. **La operación física (tomar prendas del origen y moverlas al destino)
      queda fuera del create del picking**, en pasos posteriores.

    No crea reservas, no crea transferencias ni órdenes de producción
    dentro de este endpoint:
    - Reservas, transferencias y órdenes de trabajo quedan en sus propios
    endpoints/services de otros módulos (Produccion, Transferencias) cuando el
    flujo de negocio lo necesite.
    """

    # ------------------------------------------------------------------
    # GET onboarding (4 pasos: pedido → origen/destino → existencias → OT)
    # ------------------------------------------------------------------
    @classmethod
    def onboarding_payload(
        cls,
        user,
        pedido_id=None,
        almacen_origen_id=None,
        almacen_destino_id=None,
    ):
        pedido_id = parse_pk(pedido_id)
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            return armar_payload_vacio()

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        sucursal_ids = user.sucursales_permitidas()

        (
            pedido_qs,
            pedidos_payload,
            operadores_payload,
            almacenes_qs,
            almacenes_payload,
        ) = cargar_catalogos(user, empresa, es_staff, sucursal_ids)

        almacen_origen_id = parse_pk(almacen_origen_id)
        almacen_destino_id = parse_pk(almacen_destino_id)

        almacen_origen = (
            almacenes_qs.filter(pk=almacen_origen_id).first() if almacen_origen_id else None
        )
        almacen_destino = (
            almacenes_qs.filter(pk=almacen_destino_id).first() if almacen_destino_id else None
        )

        payload = {
            "pedidos": pedidos_payload,
            "operadores": operadores_payload,
            "almacenes": almacenes_payload,
            "almacen_origen": serializar_almacen(almacen_origen),
            "almacen_destino": serializar_almacen(almacen_destino),
            "header": {
                "fecha_picking_sugerida": None,
                "folio_sugerido_preview": None,
            },
            "pedido": None,
            "picking_detalle": [],
        }

        if pedido_id is None:
            return payload

        pedido = pedido_qs.filter(pk=pedido_id).first()
        if pedido is None:
            raise ValidationError({"pedido": "Pedido no encontrado o sin acceso."})

        payload["header"] = armar_header_preview(pedido)

        if almacen_origen is None:
            origen_sugerido, destino_sugerido = sugerir_almacenes(
                pedido,
                almacen_origen_actual=None,
                almacen_destino_actual=almacen_destino,
            )
            if payload["almacen_origen"] is None:
                payload["almacen_origen"] = serializar_almacen(origen_sugerido)
                almacen_origen = origen_sugerido
            if destino_sugerido and payload["almacen_destino"] is None:
                payload["almacen_destino"] = serializar_almacen(destino_sugerido)
                almacen_destino = destino_sugerido

        if almacen_destino is None and payload["almacen_destino"] is None:
            from wms.services.picking_pipeline.catalogs import (
                resolver_apartados_safe,
            )

            almacen_apartados = resolver_apartados_safe(
                pedido.empresa_id, pedido.sucursal_id
            )
            if almacen_apartados:
                payload["almacen_destino"] = serializar_almacen(almacen_apartados)
                almacen_destino = almacen_apartados

        tallas = list(
            PedidoDetalleTalla.objects.filter(pedido_detalle__pedido=pedido)
            .select_related(
                "pedido_detalle__producto",
                "variante",
                "variante__talla",
                "variante__color",
            )
            .order_by("pedido_detalle_id", "id")
        )
        snapshots = build_snapshots(tallas, pedido, almacen_origen=almacen_origen)

        detalle_payload = []
        for snap in snapshots:
            talla = snap.talla
            requiere_bordado = bool(getattr(talla, "lleva_bordado", False))
            requiere_reflejante = bool(getattr(talla, "lleva_reflejante", False))
            requiere_corte_manga = bool(getattr(talla, "lleva_corte_manga", False))

            detalle_payload.append(
                {
                    "pedido_detalle": talla.pedido_detalle_id,
                    "pedido_detalle_talla": talla.id,
                    "producto": talla.pedido_detalle.producto_id,
                    "producto_nombre": talla.pedido_detalle.producto.nombre,
                    "producto_variante": talla.variante_id,
                    "producto_variante_nombre": (
                        str(talla.variante) if talla.variante_id else None
                    ),
                    "talla": getattr(talla.variante, "talla_id", None),
                    "talla_nombre": getattr(
                        getattr(talla.variante, "talla", None), "nombre", None
                    ),
                    "color": getattr(talla.variante, "color_id", None),
                    "color_nombre": getattr(
                        getattr(talla.variante, "color", None), "nombre", None
                    ),
                    "cantidad_pedida": str(snap.cantidad_pedida),
                    "cantidad_ya_asignada": str(snap.cantidad_asignada_historica),
                    "cantidad_ya_surtida": str(snap.cantidad_surtida_historica),
                    "cantidad_pendiente": str(snap.cantidad_pendiente),
                    "existencia_fisica": str(snap.existencia_fisica),
                    "existencia_reservada": str(snap.existencia_reservada),
                    "existencia_disponible": str(snap.existencia_disponible),
                    "maximo_picking_permitido": str(snap.maximo_picking_permitido),
                    "requiere_bordado": requiere_bordado,
                    "requiere_reflejante": requiere_reflejante,
                    "requiere_corte_manga": requiere_corte_manga,
                    "bordado_config": getattr(talla, "bordado_config", None),
                    "reflejante_config": getattr(talla, "reflejante_config", None),
                    "corte_manga_config": getattr(talla, "corte_manga_config", None),
                }
            )

        payload["pedido"] = {
            "id": pedido.id,
            "folio": pedido.folio,
            "cliente": pedido.cliente_id,
            "cliente_nombre": getattr(pedido.cliente, "nombre", None),
            "sucursal": pedido.sucursal_id,
            "sucursal_nombre": getattr(pedido.sucursal, "nombre", None),
        }
        payload["picking_detalle"] = detalle_payload
        return payload

    # ------------------------------------------------------------------
    # POST onboarding: solo crear documento Picking + Detalle
    # ------------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def handle_store(cls, data, user):
        """Crea el documento Picking (parcial o total) con sus detalles.

        Modelo tradicional: este endpoint **solo registra la intención** de
        surtir un pedido. No mueve inventario, no crea transferencias, no
        genera reservas, no crea órdenes de trabajo. El movimiento físico
        de prendas del almacén origen hacia el destino queda en flujos
        posteriores o endpoints dedicados (Transferencias, Produccion).
        """
        pedido = data.pop("pedido")
        almacen = data.pop("almacen")
        almacen_destino = data.pop("almacen_destino", None)
        operador = data.pop("operador")
        requested_rows = data.pop("picking_detalle")

        if almacen_destino is None:
            almacen_destino = resolver_apartados_obligatorio(pedido)

        # 1. Contexto (scope empresa / sucursal / permisos / origendestino)
        validar_contexto_picking(pedido, almacen, almacen_destino, operador, user)

        # 2. Resolución y validación de líneas (individual + agregada)
        requested_items = resolve_requested_items(
            pedido, requested_rows, almacen_origen=almacen
        )

        # 3. Folio picking
        folio = generate_folio(pedido.empresa, pedido.sucursal, "Picking")

        # 4. Crear encabezado Picking
        picking = Picking.objects.create(
            folio=folio,
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            operador=operador,
            almacen=almacen,
            almacen_destino=almacen_destino,
            usuario=user,
            total_lineas=len(requested_items),
            total_lineas_completas=0,
            **data,
        )

        # 5. bulk_create PickingDetalle + contar líneas completas
        picking_rows = []
        lineas_completas = 0
        for item in requested_items:
            talla = item["talla"]
            cantidad = item["cantidad"]
            pendiente = item["cantidad_pendiente"]
            if cantidad == pendiente:
                lineas_completas += 1

            picking_rows.append(
                PickingDetalle(
                    picking=picking,
                    pedido_detalle=talla.pedido_detalle,
                    pedido_detalle_talla=talla,
                    producto=talla.pedido_detalle.producto,
                    producto_variante=talla.variante,
                    cantidad_solicitada=cantidad,
                    cantidad_asignada=cantidad,
                    operador=operador,
                    observaciones=item.get("observaciones") or None,
                )
            )

        PickingDetalle.objects.bulk_create(picking_rows)
        picking.total_lineas_completas = lineas_completas
        picking.save(update_fields=["total_lineas_completas", "updated_at"])

        # 6. Return: ordenes_trabajo_generadas vacío (low-noise: las OT van por
        #    módulo Produccion endpoints dedicados).
        ordenes_trabajo_generadas = []
        return picking, ordenes_trabajo_generadas
