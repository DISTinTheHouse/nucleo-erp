from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventarios.models import inventario_reservas
from wms.services.existencia_service import ExistenciaService, SaldoExistenciaAlmacen
from wms.utils.decimales import normalizar_decimal


class ReservaInventarioService:
    _normalize = staticmethod(normalizar_decimal)

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

        # Saldo compartido por todos los ítems: dos tallas pueden caer sobre la
        # misma clave de stock (p.ej. ambas sin variante) y entonces compiten por
        # las mismas filas de Existencia. Consultarlas por ítem hacía que el
        # segundo volviera a ver el saldo original y se reservaran sobre una misma
        # fila más unidades de las que contiene.
        saldos = SaldoExistenciaAlmacen(almacen, lock=True)

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

            # Mismas filas que sumó get_existencia_agregada: la selección de la
            # clave de stock vive en ExistenciaService, para que validar y
            # reservar no puedan usar criterios distintos. El reparto descuenta el
            # saldo compartido, así que un ítem posterior de la misma clave ya ve
            # lo que se llevó el anterior.
            asignaciones, faltante = saldos.consumir(
                talla.pedido_detalle.producto_id,
                talla.variante_id,
                cantidad,
            )

            if faltante > Decimal("0"):
                producto_id = (
                    talla.variante_id
                    if talla.variante_id
                    else talla.pedido_detalle.producto_id
                )
                raise ValidationError(
                    f"No se pudo distribuir completamente la reserva para el producto/variante {producto_id} "
                    f"(faltante por asignar={faltante})."
                )

            for existencia, en_esta_ubicacion in asignaciones:
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
