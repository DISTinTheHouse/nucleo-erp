from django.db import transaction
from inventarios.models import MovimientoInventario, MovimientoInventarioDetalle

class MovimientoInventarioService:
    @staticmethod
    @transaction.atomic
    def handle_store_for_transferencia(usuario, empresa, sucursal, transferencia, transferencia_detalle_rows):
        movimiento_inventario = MovimientoInventario.objects.create(
            usuario=usuario,
            empresa=empresa,
            sucursal=sucursal,
            tipo_movimiento='TRANSFERENCIA',
            observaciones=transferencia.observaciones,
            transferencia=transferencia
        )

        bulk_rows = [
            MovimientoInventarioDetalle(movimiento_inventario=movimiento_inventario, **row)
            for row in transferencia_detalle_rows
        ]

        MovimientoInventarioDetalle.objects.bulk_create(bulk_rows)
        return movimiento_inventario.id
    
    