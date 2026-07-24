from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventarios.models import inventario_reservas
from wms.services.existencia_service import ExistenciaService


class ReservaInventarioService:
    @staticmethod
    def create_for_picking(pedido, almacen, requested_items, user):
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
            cantidad = item["cantidad"]
            existencia = ExistenciaService.get_existencia(
                almacen=almacen,
                producto=talla.pedido_detalle.producto,
                producto_variante=talla.variante,
            )
            if not existencia:
                producto_id = (
                    talla.variante_id
                    if talla.variante_id
                    else talla.pedido_detalle.producto_id
                )
                raise ValidationError(
                    f"No hay existencia disponible para reservar el producto/variante con id {producto_id}."
                )
            if existencia.cantidad < talla.cantidad:
                producto_id = (
                    talla.variante_id
                    if talla.variante_id
                    else talla.pedido_detalle.producto_id
                )
                raise ValidationError(
                    f"Inventario insuficiente para reservar el producto/variante con id {producto_id}."
                )

            reservas.append(
                inventario_reservas(
                    empresa=pedido.empresa,
                    sucursal=pedido.sucursal,
                    pedido_detalle=talla.pedido_detalle,
                    pedido_detalle_talla=talla,
                    existencia=existencia,
                    almacen=almacen,
                    ubicacion=existencia.ubicacion,
                    cantidad=cantidad,
                    usuario=user,
                    observaciones="Reserva generada automáticamente desde picking.",
                )
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
