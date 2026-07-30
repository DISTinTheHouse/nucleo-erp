from django.db import transaction
from rest_framework.exceptions import ValidationError
from produccion.models import OrdenesBordado, OrdenBordadoDetalle
from produccion.utils.folios import generate_ob_folio
from ventas.models import Pedido, PedidoDetalleTalla

class OrdenBordadoService:

    @staticmethod
    def _validar_contexto(pedido, user):
        """Scope empresa/sucursal del pedido contra el usuario que solicita.

        Mismo criterio y mismos mensajes que
        ``wms.services.picking_pipeline.context.validar_contexto_picking``:
        el ``pedido`` llega como id crudo desde el body y el serializer no lo
        acota a la empresa del usuario, así que sin esta puerta un usuario de
        la empresa A podía enviar un pedido de la empresa B y el service
        estampaba la orden con ``pedido.empresa`` —creando documento, gastando
        un folio de la serie ajena y devolviendo datos de negocio de B—.

        Se ejecuta **antes de cualquier escritura** (en particular antes de
        ``generate_ob_folio``) para que un rechazo no consuma consecutivo.
        """
        empresa = getattr(user, "empresa", None)

        if empresa is None:
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if pedido.empresa_id != empresa.pk:
            raise ValidationError("El pedido no pertenece a la empresa del usuario.")

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        if not es_staff and pedido.sucursal_id not in user.sucursales_permitidas():
            raise ValidationError(
                "No tiene acceso a la sucursal del pedido para generar la orden "
                "de bordado."
            )

    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.get("pedido")

        OrdenBordadoService._validar_contexto(pedido, user)

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
