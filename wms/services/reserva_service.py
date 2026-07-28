from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventarios.models import Existencia, inventario_reservas
from wms.services.existencia_service import ExistenciaService


class ReservaInventarioService:
    @staticmethod
    def _normalize(value):
        return Decimal(str(value or "0"))

    @classmethod
    @transaction.atomic
    def create_for_picking(cls, pedido, almacen, requested_items, user):
        reserva_activa = inventario_reservas.objects.filter(
            pedido_detalle__pedido=pedido,
            estado=inventario_reservas.Estado.ACTIVA,
        ).exists()
        if reserva_activa:
            raise ValidationError(
                "El pedido ya tiene reservas de inventario activas."
            )

        reservas = []
        for item in requested_items:
            talla = item["talla"]
            cantidad = cls._normalize(item["cantidad"])

            if cantidad <= Decimal("0"):
                raise ValidationError(
                    "Cada línea solicitada debe tener una cantidad asignada mayor a cero."
                )

            existencia_total, _, existencia_disponible = (
                ExistenciaService.get_existencia_agregada(
                    almacen=almacen,
                    producto=talla.pedido_detalle.producto,
                    producto_variante=talla.variante,
                )
            )
            existencia_total = cls._normalize(existencia_total)
            existencia_disponible = cls._normalize(existencia_disponible)

            if existencia_total <= Decimal("0"):
                producto_id = (
                    talla.variante_id
                    if talla.variante_id
                    else talla.pedido_detalle.producto_id
                )
                raise ValidationError(
                    f"No hay existencia disponible para reservar el producto/variante con id {producto_id}."
                )

            if existencia_disponible < cantidad:
                producto_id = (
                    talla.variante_id
                    if talla.variante_id
                    else talla.pedido_detalle.producto_id
                )
                raise ValidationError(
                    f"Inventario insuficiente para reservar el producto/variante con id {producto_id} "
                    f"(disponible={existencia_disponible}, solicitado={cantidad})."
                )

            filters = {"almacen": almacen}
            if talla.variante_id:
                filters["producto_variante"] = talla.variante_id
            else:
                filters["producto"] = talla.pedido_detalle.producto_id
            existencia_rows = list(
                Existencia.objects.select_for_update()
                .filter(**filters)
                .order_by("pk")
            )

            pendiente_asignar = cantidad
            for existencia in existencia_rows:
                if pendiente_asignar <= Decimal("0"):
                    break
                en_esta_ubicacion = min(
                    cls._normalize(existencia.cantidad),
                    pendiente_asignar,
                )
                if en_esta_ubicacion <= Decimal("0"):
                    continue

                reservas.append(
                    inventario_reservas(
                        empresa=pedido.empresa,
                        sucursal=pedido.sucursal,
                        pedido_detalle=talla.pedido_detalle,
                        pedido_detalle_talla=talla,
                        existencia=existencia,
                        almacen=almacen,
                        ubicacion=existencia.ubicacion,
                        cantidad=en_esta_ubicacion,
                        usuario=user,
                        observaciones=(
                            "Reserva generada automáticamente desde picking "
                            f"(distribuida en ubicaciones)."
                        ),
                    )
                )
                pendiente_asignar -= en_esta_ubicacion

            if pendiente_asignar > Decimal("0"):
                producto_id = (
                    talla.variante_id
                    if talla.variante_id
                    else talla.pedido_detalle.producto_id
                )
                raise ValidationError(
                    f"No se pudo distribuir completamente la reserva para el producto/variante {producto_id} "
                    f"(faltante por asignar={pendiente_asignar})."
                )

        return inventario_reservas.objects.bulk_create(reservas)

    @staticmethod
    def apply_to_picking(reservas, picking, transferencia):
        fecha_aplicacion = timezone.now()
        for reserva in reservas:
            reserva.picking = picking
            reserva.transferencia = transferencia
            reserva.estado = inventario_reservas.Estado.APLICADA
            reserva.fecha_aplicacion = fecha_aplicacion

        inventario_reservas.objects.bulk_update(
            reservas,
            ["picking", "transferencia", "estado", "fecha_aplicacion"],
        )
