from django.db import transaction
from django.db.models import Count
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesReflejante, OrdenReflejanteDetalle
from produccion.utils.folios import generate_or_folio
from ventas.models import PedidoDetalleTalla


class OrdenReflejanteDuplicada409(APIException):
    status_code = 409
    default_detail = "Ya existe una orden de reflejante activa para este pedido."
    default_code = "orden_reflejante_duplicada"


class OrdenReflejanteService:

    @staticmethod
    def _validar_contexto(pedido, user):
        """Scope empresa/sucursal del pedido contra el usuario que solicita.

        Mismo criterio y mismos mensajes que
        ``OrdenBordadoService._validar_contexto``: sin esta puerta un usuario de
        la empresa A podía enviar un pedido de la empresa B y el service
        estampaba la orden con ``pedido.empresa`` —creando documento, gastando
        un folio de la serie ajena y devolviendo datos de negocio de B—.

        Se ejecuta **antes de cualquier escritura** (en particular antes de
        ``generate_or_folio``) para que un rechazo no consuma consecutivo.
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
                "de reflejante."
            )

    @staticmethod
    def buscar_existente_full_match(pedido):
        """Devuelve OrdenesReflejante activa si ya cubre 100% de las tallas con lleva_reflejante.

        Regla SAFE minimalista: misma cantidad de detalle_tallas que el pedido.
        Si negocio decide habilitar fraccionamiento (OR parcial), esta función
        regresa None y se permite una segunda OR.
        """
        tallas_esperadas_qty = (
            PedidoDetalleTalla.objects.filter(
                pedido_detalle__pedido=pedido,
                lleva_reflejante=True,
                cantidad__gt=0,
            ).count()
        )
        if tallas_esperadas_qty == 0:
            return None

        or_match = (
            OrdenesReflejante.objects.filter(
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
        return or_match

    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.get("pedido")

        OrdenReflejanteService._validar_contexto(pedido, user)

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
                "err": "El pedido no tiene detalles con reflejante para generar la orden."
            })

        existente = OrdenReflejanteService.buscar_existente_full_match(pedido)
        if existente is not None:
            raise OrdenReflejanteDuplicada409({
                "err": (
                    "Ya existe una orden de reflejante activa para este pedido con el 100% "
                    "de las prendas. Si requiere dividir el reflejante, contacte a producción."
                ),
                "orden_reflejante_existente": {
                    "id": existente.id,
                    "folio": existente.folio_reflejante,
                    "pedido": existente.pedido_id,
                    "estado": existente.get_estatus_reflejante_display()
                    if hasattr(existente, "get_estatus_reflejante_display")
                    else existente.estatus_reflejante,
                },
            })

        folio_reflejante = generate_or_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_reflejante = OrdenesReflejante.objects.create(
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            folio_reflejante=folio_reflejante,
            usuario_asignado=user,
            prioridad=data.get("prioridad", 1),
            observaciones=data.get("observaciones"),
        )

        bulk_data = [
            OrdenReflejanteDetalle(
                orden_r=orden_reflejante,
                pedido_detalle=detalle_talla.pedido_detalle,
                producto_id=detalle_talla.pedido_detalle.producto_id,
                cantidad=detalle_talla.cantidad,
                talla=detalle_talla.talla,
            )
            for detalle_talla in detalle_tallas
        ]

        OrdenReflejanteDetalle.objects.bulk_create(bulk_data)
        return orden_reflejante
