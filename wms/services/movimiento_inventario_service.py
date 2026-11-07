from django.db import transaction
from inventarios.models import MovimientoInventario, MovimientoInventarioDetalle
from inventarios.services import costeo

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
        # Mueve stock entre almacenes de la misma empresa: no cambia el promedio (#809).
        costeo.costear(empresa.pk if hasattr(empresa, "pk") else empresa, [(d, 0, None) for d in bulk_rows], costeo.Origen.TRANSFERENCIA)
        return movimiento_inventario.id
    
    