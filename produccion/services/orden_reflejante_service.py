from django.db import transaction
from rest_framework.exceptions import ValidationError
from produccion.utils.folios import generate_or_folio
from produccion.models import OrdenesReflejante, OrdenReflejanteDetalle
from ventas.models import PedidoDetalleTalla

class OrdenReflejanteService:
    def save(data, user):
        pedido = data.pop("pedido")
        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

        detalle_tallas = list(
            PedidoDetalleTalla.objects.select_related("pedido_detalle", "talla").filter(
                pedido_detalle__pedido_id=pedido.id,
                lleva_reflejante=True,
            )
        )

        if not detalle_tallas:
             raise ValidationError({
                "err": "El pedido no tiene detalles con bordado para generar la orden."
            })

        folio_reflejante = generate_or_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_reflejante = OrdenesReflejante.objects.create(
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            folio_reflejante=folio_reflejante,
            usuario_asignado=user,
            **data
        )

        bulk_data = [
            OrdenReflejanteDetalle(
                orden_r=orden_reflejante,
                pedido_detalle=detalle_talla.pedido_detalle,
                producto_id=detalle_talla.pedido_detalle.producto_id,
                cantidad=detalle_talla.cantidad,
                talla=detalle_talla.talla
            )
            for detalle_talla in detalle_tallas
        ]

        OrdenReflejanteDetalle.objects.bulk_create(bulk_data)
        return orden_reflejante
