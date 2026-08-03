from django.db import transaction
from django.db.models import Count
from rest_framework.exceptions import ValidationError, APIException
from ventas.models import PedidoDetalleTalla
from produccion.models import OrdenesCorteManga, OrdenCorteMangaDetalle
from produccion.utils.folios import generate_ocm_folio


class OrdenCorteMangaDuplicada409(APIException):
    status_code = 409
    default_detail = "Ya existe una orden de corte de manga activa para este pedido."
    default_code = "orden_corte_manga_duplicada"


class OrdenCorteMangaService:

    @staticmethod
    def _validar_contexto(pedido, user):
        """Scope empresa/sucursal del pedido contra el usuario que solicita.

        Mismo criterio y mismos mensajes que
        ``OrdenBordadoService._validar_contexto`` / ``OrdenReflejanteService._validar_contexto``.
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
                "de corte de manga."
            )

    @staticmethod
    def buscar_existente_full_match(pedido):
        """Devuelve OrdenesCorteManga activa si ya cubre 100% de las tallas con lleva_corte_manga.

        Regla SAFE minimalista: misma cantidad de detalle_tallas que el pedido.
        Si negocio decide habilitar fraccionamiento (OCM parcial), esta función
        regresa None y se permite una segunda OCM.
        """
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
                activo=True,
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
        pedido = data.get("pedido")

        OrdenCorteMangaService._validar_contexto(pedido, user)

        sucursal = user.sucursal_default
        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

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
            raise OrdenCorteMangaDuplicada409({
                "err": (
                    "Ya existe una orden de corte de manga activa para este pedido con el 100% "
                    "de las prendas. Si requiere dividir el corte, contacte a producción."
                ),
                "orden_corte_manga_existente": {
                    "id": existente.id,
                    "folio": existente.folio_ocm,
                    "pedido": existente.pedido_id,
                    "estado": existente.get_estatus_corte_display()
                    if hasattr(existente, "get_estatus_corte_display")
                    else existente.estatus_corte,
                },
            })

        folio_ocm = generate_ocm_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_corte_manga = OrdenesCorteManga.objects.create(
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            folio_ocm=folio_ocm,
            usuario_asignado=user,
            prioridad=data.get("prioridad", 1),
            observaciones=data.get("observaciones"),
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
