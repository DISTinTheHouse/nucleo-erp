from django.db import transaction
from rest_framework.exceptions import ValidationError
from produccion.models import OrdenesBordado, OrdenBordadoDetalle
from produccion.utils.folios import generate_ob_folio
from ventas.models import Pedido, PedidoDetalleTalla

class OrdenBordadoService:

    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.get("pedido")
        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

        detalle_tallas = list(
            PedidoDetalleTalla.objects.select_related("pedido_detalle", "talla").filter(
                pedido_detalle__pedido_id=pedido.id,
                lleva_bordado=True,
            )
        )

        if not detalle_tallas:
            raise ValidationError({
                 "err": "El pedido no tiene detalles con bordado para generar la orden."
            })

        folio_bordado = generate_ob_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_bordado = OrdenesBordado.objects.create(
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            folio_bordado=folio_bordado,
            usuario_asignado=user,
            prioridad=data.get("prioridad", 1),
            observaciones=data.get("observaciones"),
        )

        bulk_data = [
            OrdenBordadoDetalle(
                ob=orden_bordado,
                pedido_detalle=detalle_talla.pedido_detalle,
                producto_id=detalle_talla.pedido_detalle.producto_id,
                cantidad=detalle_talla.cantidad,
                talla=detalle_talla.talla,
            )
            for detalle_talla in detalle_tallas
        ]

        OrdenBordadoDetalle.objects.bulk_create(bulk_data)

        return orden_bordado
