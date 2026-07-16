from django.db import transaction
from rest_framework.exceptions import ValidationError
from wms.models import Transferencia, TransferenciaDetalle
from inventarios.models import Existencia
from wms.utils.folios import generate_transferencia_folio
from wms.services.existencia_service import ExistenciaService
from wms.services.movimiento_inventario_service import MovimientoInventarioService


class TransferenciaService:
    @staticmethod
    @transaction.atomic
    def handle_store(data, user):
        almacen_origen = data["almacen_origen"]
        almacen_destino = data["almacen_destino"]
        empresa = user.empresa
        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError("El usuario no tiene una sucursal asignada.")

        transferencia_detalle_rows = data.pop("transferencia_detalle")

        movimientos = []

        # 1. Validar inventario y obtener existencias
        for row in transferencia_detalle_rows:
            producto = row.get("producto")
            producto_variante = row.get("producto_variante")
            cantidad = row["cantidad"]

            existencia_origen = ExistenciaService.get_existencia(
                almacen_origen,
                producto,
                producto_variante,
            )

            if not existencia_origen:
                producto_id = producto.id if producto else producto_variante.id
                raise ValidationError(
                    f"No hay existencia del producto/variante con id {producto_id} en el almacén de origen."
                )

            if existencia_origen.cantidad < cantidad:
                producto_id = producto.id if producto else producto_variante.id
                raise ValidationError(
                    f"Inventario insuficiente del producto/variante con id {producto_id}"
                )

            existencia_destino = ExistenciaService.get_existencia(
                almacen_destino,
                producto,
                producto_variante,
            )

            movimientos.append(
                {
                    "row": row,
                    "cantidad": cantidad,
                    "existencia_origen": existencia_origen,
                    "existencia_destino": existencia_destino,
                }
            )

        # 2. Crear transferencia
        transferencia = Transferencia.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            folio=generate_transferencia_folio(empresa, sucursal),
            usuario=user,
            **data,
        )

        detalles = []

        # 3. Actualizar existencias y preparar detalles
        for movimiento in movimientos:
            row = movimiento["row"]
            cantidad = movimiento["cantidad"]

            existencia_origen = movimiento["existencia_origen"]
            existencia_destino = movimiento["existencia_destino"]

            existencia_origen.cantidad -= cantidad

            if existencia_destino:
                existencia_destino.cantidad += cantidad
            else:
                existencia_destino = Existencia(
                    producto=row.get("producto"),
                    producto_variante=row.get("producto_variante"),
                    almacen=almacen_destino,
                    stock=0,
                    cantidad=cantidad,
                )

            existencia_origen.save(update_fields=["cantidad"])
            existencia_destino.save()

            detalles.append(
                TransferenciaDetalle(
                    transferencia=transferencia,
                    **row,
                )
            )

        TransferenciaDetalle.objects.bulk_create(detalles)

        # 4. Registrar movimientos
        MovimientoInventarioService.handle_store_for_transferencia(
            usuario=user,
            empresa=empresa,
            sucursal=sucursal,
            transferencia=transferencia,
            transferencia_detalle_rows=transferencia_detalle_rows,
        )

        return transferencia
