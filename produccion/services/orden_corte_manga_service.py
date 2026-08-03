from django.db import transaction
from django.db.models import Count
from rest_framework.exceptions import ValidationError
from ventas.models import PedidoDetalleTalla
from produccion.models import OrdenesCorteManga, OrdenCorteMangaDetalle
from produccion.utils.folios import generate_ocm_folio

class OrdenCorteMangaService:
    @staticmethod
    def _validar_contexto(pedido, user):
        empresa = getattr(user, "empresa", None)
        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

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
    def buscar_existente_full_match(pedido):
        tallas_esperadas_qty = (
            PedidoDetalleTalla.objects.filter(
                pedido_detalle__pedido=pedido,
                lleva_corte_manga=True,
                cantidad__gt=0,
            ).count()
        )

        if tallas_esperadas_qty == 0:
            return None

        ocm_match = (
            OrdenesCorteManga.objects.filter(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                activo=True
        )
        .annotate(detalle_count=Count("detalles"))
        .filter(detalle_count=tallas_esperadas_qty)
        .order_by("-id")
        .first()
        )

        return ocm_match
        
    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.pop("pedido")
        OrdenCorteMangaService._validar_contexto(pedido, user)

        detalle_tallas = list(
            PedidoDetalleTalla.objects.select_related("pedido_detalle", "talla").filter(
                pedido_detalle__pedido_id=pedido.id,
                lleva_corte_manga=True,
            )
        )

        if not detalle_tallas:
            raise ValidationError({
                 "err": "El pedido no tiene detalles con corte de manga para generar la orden."
            })

        existente = OrdenCorteMangaService.buscar_existente_full_match(pedido)

        if existente is not None:
            raise ValidationError({
                "err": "Ya existe una orden de corte de manga activa para este pedido.",
                "orden_bordado_existente": {
                    "id": existente.id,
                }
            })

        ocm_folio = generate_ocm_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_corte_manga = OrdenesCorteManga.objects.create(
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            folio_ocm=ocm_folio,
            usuario_asignado=user,
            **data
        )

        bulk_data = [
            OrdenCorteMangaDetalle(
                ocm=orden_corte_manga,
                pedido_detalle=detalle_talla.pedido_detalle,
                producto_id=detalle_talla.pedido_detalle.producto_id,
                cantidad=detalle_talla.cantidad,
                talla=detalle_talla.talla,
            )
            for detalle_talla in detalle_tallas
        ]

        OrdenCorteMangaDetalle.objects.bulk_create(bulk_data)

        return orden_corte_manga







        





        

        




        
