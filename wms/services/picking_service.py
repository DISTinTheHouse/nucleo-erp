from django.db import transaction
from rest_framework.exceptions import ValidationError

from inventarios.models import Almacen
from ventas.models import PedidoDetalleTalla
from wms.models import Picking, PickingDetalle
from wms.services.transferencia_service import TransferenciaService
from wms.utils.folios import generate_folio

class PickingService:
    @staticmethod
    @transaction.atomic
    def handle_store(data, user):
        pedido = data.pop("pedido")
        almacen = data.pop("almacen")
        operador = data.pop("operador")
        empresa = getattr(user, "empresa", None)

        if empresa is None:
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if pedido.empresa_id != empresa.pk:
            raise ValidationError("El pedido no pertenece a la empresa del usuario.")
        if almacen.empresa_id and almacen.empresa_id != pedido.empresa_id:
            raise ValidationError("El almacén no pertenece a la empresa del pedido.")
        if almacen.sucursal_id and almacen.sucursal_id != pedido.sucursal_id:
            raise ValidationError("El almacén no pertenece a la sucursal del pedido.")
        if getattr(operador, "empresa_id", None) != pedido.empresa_id:
            raise ValidationError("El operador no pertenece a la empresa del pedido.")
        if not getattr(operador, "is_active", False):
            raise ValidationError("El operador no está activo.")

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        if not es_staff:
            sucursales_permitidas = set(user.sucursales.values_list("pk", flat=True))
            if user.sucursal_default_id:
                sucursales_permitidas.add(user.sucursal_default_id)
            if pedido.sucursal_id not in sucursales_permitidas:
                raise ValidationError(
                    "No tiene acceso a la sucursal del pedido para generar el picking."
                )
            if almacen.sucursal_id and almacen.sucursal_id not in sucursales_permitidas:
                raise ValidationError(
                    "No tiene acceso a la sucursal del almacén seleccionado."
                )

        picking_existente = (
            Picking.objects.filter(pedido=pedido)
            .exclude(estado=Picking.Estado.CANCELADO)
            .exists()
        )
        if picking_existente:
            raise ValidationError("El pedido ya tiene un picking activo.")

        tallas = list(
            PedidoDetalleTalla.objects.filter(pedido_detalle__pedido=pedido)
            .select_related("pedido_detalle__producto", "variante")
            .order_by("pedido_detalle_id", "id")
        )
        if not tallas:
            raise ValidationError("El pedido no tiene líneas para generar picking.")

        almacen_apartados = (
            Almacen.objects.filter(
                nombre="APARTADOS",
                empresa_id=pedido.empresa_id,
                sucursal_id=pedido.sucursal_id,
            )
            .order_by("id_almacen")
            .first()
        )
        if not almacen_apartados:
            raise ValidationError(
                "No existe el almacén APARTADOS para la empresa y sucursal del pedido."
            )

        # Crear transferencia
        # Al crear la transferencia se valida el stock y se genera el movimiento en inventario
        transferencia_data = {
            "almacen_origen": almacen,
            "almacen_destino": almacen_apartados,
            "observaciones": "Generada desde picking",
            "transferencia_detalle": [
                {
                    "producto": talla.pedido_detalle.producto,
                    "producto_variante": talla.variante,
                    "cantidad": talla.cantidad,
                }
                for talla in tallas
            ],
        }

        TransferenciaService.handle_store(transferencia_data, user)
        
        folio = generate_folio(pedido.empresa, pedido.sucursal, "Picking")
        picking = Picking.objects.create(
            folio=folio,
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            operador=operador, 
            almacen=almacen, 
            usuario=user, 
            total_lineas=len(tallas),
            **data
        )

        picking_rows = [
            PickingDetalle(
                picking=picking,
                pedido_detalle=talla.pedido_detalle,
                producto=talla.pedido_detalle.producto,
                producto_variante=talla.variante,
                cantidad_asignada=talla.cantidad,
                cantidad_solicitada=talla.cantidad,
                operador=operador,
            )
            for talla in tallas
        ]

        PickingDetalle.objects.bulk_create(picking_rows)
        return picking
