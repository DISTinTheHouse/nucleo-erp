from django.db import transaction
from rest_framework.exceptions import ValidationError
from wms.models import Transferencia, TransferenciaDetalle
from inventarios.models import Existencia
from wms.utils.folios import generate_folio
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

        # Validación de tenencia de los almacenes. Los campos almacen_origen/
        # almacen_destino del serializer no están acotados (queryset=Almacen.objects
        # .all()); sin esto un usuario podría referenciar almacenes de otra empresa o
        # de una sucursal a la que no tiene acceso. La lectura ya se acota en
        # TransferenciaViewSet.get_queryset(); esto cierra el lado de escritura. No se
        # exige que origen y destino compartan sucursal: una transferencia
        # inter-sucursal es legítima siempre que el usuario tenga acceso a ambas.
        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )

        # Empresa: si el almacén tiene empresa, debe coincidir con la del usuario (la
        # que se timbra en la transferencia). No hay caso legítimo cross-empresa.
        # Almacen.empresa es nullable; los almacenes sin empresa no se comprueban aquí
        # (mismo criterio que el perform_create de inventarios).
        if empresa is not None:
            if almacen_origen.empresa_id and almacen_origen.empresa_id != empresa.pk:
                raise ValidationError(
                    "El almacén de origen pertenece a una empresa distinta a la del usuario."
                )
            if almacen_destino.empresa_id and almacen_destino.empresa_id != empresa.pk:
                raise ValidationError(
                    "El almacén de destino pertenece a una empresa distinta a la del usuario."
                )

        # Sucursal: el usuario debe tener acceso a la sucursal de cada almacén.
        # Superuser y admin de empresa ven todas las sucursales de su empresa y se
        # saltan la comprobación (mismo criterio que get_queryset()). El conjunto
        # permitido son las sucursales del M2M user.sucursales más la sucursal por
        # defecto (con la que se timbra la transferencia), para no bloquear a usuarios
        # cuyo M2M esté vacío pero con sucursal_default asignada. Almacen.sucursal es
        # nullable; un almacén sin sucursal no se comprueba.
        if not es_staff:
            sucursales_permitidas = set(user.sucursales.values_list("pk", flat=True))
            if user.sucursal_default_id:
                sucursales_permitidas.add(user.sucursal_default_id)
            if (
                almacen_origen.sucursal_id
                and almacen_origen.sucursal_id not in sucursales_permitidas
            ):
                raise ValidationError(
                    "No tiene acceso a la sucursal del almacén de origen."
                )
            if (
                almacen_destino.sucursal_id
                and almacen_destino.sucursal_id not in sucursales_permitidas
            ):
                raise ValidationError(
                    "No tiene acceso a la sucursal del almacén de destino."
                )

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
            folio=generate_folio(empresa, sucursal, 'Transferencia'),
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
