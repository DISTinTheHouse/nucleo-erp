from rest_framework.exceptions import ValidationError
from django.db import transaction
from wms.models import Picking, PickingDetalle
from ventas.models import PedidoDetalleTalla
from inventarios.models import Almacen
from catalogo.models import Producto, ProductoVariante
from wms.utils.folios import generate_folio
from wms.services.transferencia_service import TransferenciaService

class PickingService:
    @staticmethod
    @transaction.atomic
    def handle_store(data, user):
        pedido = data.pop("pedido")
        almacen = data.pop("almacen")
        operador = data.pop("operador")

        # Obtener cantidad requerida
        tallas = PedidoDetalleTalla.objects.filter(
            pedido_detalle__pedido=pedido
        ).values(
            "pedido_detalle__producto_id",
            "pedido_detalle__id",
            "cantidad",
            "variante_id",
        )

        almacen_apartados = Almacen.objects.filter(nombre="APARTADOS").first()
        if not almacen_apartados: raise ValidationError("No existe el almacen APARTADOS")

        # Crear transferencia
        # Al crear la transferencia se valida el stock y se genera el movimiento en inventario
        transferencia_data = {
            "almacen_origen": almacen,
            "almacen_destino": almacen_apartados,
            "observaciones": "Generada desde picking",
            "transferencia_detalle": [
                {
                    "producto": Producto.objects.filter(pk=talla["pedido_detalle__producto_id"]).first(),
                    "producto_variante": ProductoVariante.objects.filter(pk=talla["variante_id"]).first(),
                    "cantidad": talla["cantidad"],
                }
                for talla in tallas
            ],
        }

        TransferenciaService.handle_store(transferencia_data, user)
        
        # TODO: Generar picking
        folio = generate_folio(user.empresa, user.sucursal_default, "Picking")
        picking = Picking.objects.create(
            folio=folio,
            empresa=user.empresa,
            sucursal=user.sucursal_default,
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
                pedido_detalle_id=talla["pedido_detalle__id"],
                producto_id=talla["pedido_detalle__producto_id"],
                producto_variante_id=talla["variante_id"],
                cantidad_solicitada=talla["cantidad"],
            )
            for talla in tallas
        ]

        PickingDetalle.objects.bulk_create(picking_rows)
        return picking