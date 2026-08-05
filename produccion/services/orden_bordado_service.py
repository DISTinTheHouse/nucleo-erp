from django.db import transaction
from django.db.models import Count
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesBordado, OrdenBordadoDetalle
from produccion.services.common import (
    crear_orden_con_guardia_duplicado,
    payload_duplicada,
    revisar_empresa,
    tallas_orden_trabajo_qs,
)
from produccion.utils.folios import generate_ob_folio


class OrdenBordadoDuplicada409(APIException):
    status_code = 409
    default_detail = "Ya existe una orden de bordado activa para este pedido."
    default_code = "orden_bordado_duplicada"


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
        resultado = revisar_empresa(user, pedido)
        if resultado == "sin_empresa":
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if resultado == "otra_empresa":
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
    def _tallas_bordado_qs(pedido_id):
        return tallas_orden_trabajo_qs(pedido_id, "lleva_bordado")

    @staticmethod
    def _payload_duplicada(existente):
        return payload_duplicada(
            existente,
            folio_field="folio_bordado",
            estatus_display="get_estatus_bordado_display",
            estatus_field="estatus_bordado",
            payload_key="orden_bordado_existente",
            tipo_label="bordado",
            dividir_label="el bordado",
        )

    @staticmethod
    def buscar_existente_full_match(pedido):
        """Devuelve OrdenesBordado activa si ya cubre 100% de las tallas con lleva_bordado.

        Regla SAFE minimalista: misma cantidad de detalle_tallas que el pedido.
        Si negocio decide habilitar fraccionamiento (OB parcial), esta función
        regresa None y se permite una segunda OB.
        """
        tallas_esperadas_qty = OrdenBordadoService._tallas_bordado_qs(pedido.id).count()
        if tallas_esperadas_qty == 0:
            return None

        ob_match = (
            OrdenesBordado.objects.filter(
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
        return ob_match

    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.get("pedido")

        OrdenBordadoService._validar_contexto(pedido, user)

        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

        detalle_tallas = list(
            OrdenBordadoService._tallas_bordado_qs(pedido.id).select_related(
                "pedido_detalle", "talla"
            )
        )

        if not detalle_tallas:
            raise ValidationError({
                 "err": "El pedido no tiene detalles con bordado para generar la orden."
            })

        existente = OrdenBordadoService.buscar_existente_full_match(pedido)
        if existente is not None:
            raise OrdenBordadoDuplicada409(
                OrdenBordadoService._payload_duplicada(existente)
            )

        folio_bordado = generate_ob_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_bordado = crear_orden_con_guardia_duplicado(
            OrdenesBordado,
            pedido,
            dict(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_bordado=folio_bordado,
                usuario_asignado=user,
                prioridad=data.get("prioridad", 1),
                observaciones=data.get("observaciones"),
            ),
            OrdenBordadoDuplicada409,
            OrdenBordadoService._payload_duplicada,
        )

        bulk_data = []
        for detalle_talla in detalle_tallas:
            cfg = detalle_talla.bordado_config or {}
            ubicaciones = cfg.get("ubicaciones") or []
            primera_ubicacion = (
                ubicaciones[0] if isinstance(ubicaciones, list) and ubicaciones else {}
            )
            posicion = (
                cfg.get("posicion")
                or primera_ubicacion.get("codigo")
                or primera_ubicacion.get("nombre")
                or None
            )
            bulk_data.append(OrdenBordadoDetalle(
                ob=orden_bordado,
                pedido_detalle=detalle_talla.pedido_detalle,
                producto_id=detalle_talla.pedido_detalle.producto_id,
                cantidad=detalle_talla.cantidad,
                talla=detalle_talla.talla,
                color=getattr(detalle_talla.pedido_detalle, "color", None),
                posicion_bordado=posicion,
                colores_hilo=(
                    int(primera_ubicacion.get("colores_hilo") or cfg.get("colores_hilo") or 0)
                ),
                puntadas=int(cfg.get("puntadas") or primera_ubicacion.get("puntadas") or 0),
            ))

        OrdenBordadoDetalle.objects.bulk_create(bulk_data)

        return orden_bordado

