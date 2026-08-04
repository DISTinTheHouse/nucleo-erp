from django.db import transaction
from django.db.models import Count
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesCorteManga, OrdenCorteMangaDetalle
from produccion.services.common import (
    crear_orden_con_guardia_duplicado,
    payload_duplicada,
    revisar_empresa,
    tallas_orden_trabajo_qs,
)
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
                "de corte de manga."
            )

    @staticmethod
    def _tallas_corte_manga_qs(pedido_id):
        return tallas_orden_trabajo_qs(pedido_id, "lleva_corte_manga")

    @staticmethod
    def _payload_duplicada(existente):
        return payload_duplicada(
            existente,
            folio_field="folio_ocm",
            estatus_display="get_estatus_corte_display",
            estatus_field="estatus_corte",
            payload_key="orden_corte_manga_existente",
            tipo_label="corte de manga",
            dividir_label="el corte",
        )

    @staticmethod
    def buscar_existente_full_match(pedido):
        """Devuelve OrdenesCorteManga activa si ya cubre 100% de las tallas con lleva_corte_manga.

        Regla SAFE minimalista: misma cantidad de detalle_tallas que el pedido.
        Si negocio decide habilitar fraccionamiento (OCM parcial), esta función
        regresa None y se permite una segunda OCM.
        """
        tallas_esperadas_qty = OrdenCorteMangaService._tallas_corte_manga_qs(
            pedido.id
        ).count()
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
            OrdenCorteMangaService._tallas_corte_manga_qs(pedido.id).select_related(
                "pedido_detalle", "talla"
            )
        )

        if not detalle_tallas:
             raise ValidationError({
                "err": "El pedido no tiene detalles con corte de manga para generar la orden."
            })

        existente = OrdenCorteMangaService.buscar_existente_full_match(pedido)
        if existente is not None:
            raise OrdenCorteMangaDuplicada409(
                OrdenCorteMangaService._payload_duplicada(existente)
            )

        folio_ocm = generate_ocm_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_corte_manga = crear_orden_con_guardia_duplicado(
            OrdenesCorteManga,
            pedido,
            dict(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_ocm=folio_ocm,
                usuario_asignado=user,
                prioridad=data.get("prioridad", 1),
                observaciones=data.get("observaciones"),
            ),
            OrdenCorteMangaDuplicada409,
            OrdenCorteMangaService._payload_duplicada,
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
