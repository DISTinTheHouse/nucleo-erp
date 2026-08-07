from django.db import transaction
from django.db.models import Count, Sum
from rest_framework.exceptions import ValidationError, APIException
from produccion.models import OrdenesReflejante, OrdenReflejanteDetalle
from produccion.services.common import cantidades_asignadas, crear_orden_con_guardia_duplicado, payload_duplicada, revisar_empresa, tallas_orden_trabajo_qs
from produccion.utils.folios import generate_or_folio


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
                "de reflejante."
            )

    @staticmethod
    def _tallas_reflejante_qs(pedido_id):
        return tallas_orden_trabajo_qs(pedido_id, "lleva_reflejante")

    @staticmethod
    def _payload_duplicada(existente):
        return payload_duplicada(
            existente,
            folio_field="folio_reflejante",
            estatus_display="get_estatus_reflejante_display",
            estatus_field="estatus_reflejante",
            payload_key="orden_reflejante_existente",
            tipo_label="reflejante",
            dividir_label="el reflejante",
        )

    @staticmethod
    def buscar_existente_full_match(pedido):
        """Devuelve OrdenesReflejante activa si ya cubre 100% de las tallas con lleva_reflejante.

        Regla SAFE minimalista: misma cantidad de detalle_tallas que el pedido.
        Si negocio decide habilitar fraccionamiento (OR parcial), esta función
        regresa None y se permite una segunda OR.
        """
        tallas_esperadas_qty = OrdenReflejanteService._tallas_reflejante_qs(pedido.id).count()
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
    def cantidades_asignadas_por_pedidos(pedido_ids):
        """``common.cantidades_asignadas`` para ORs; ver allí el contrato.

        La FK a la orden padre es ``orden_r`` (``OrdenReflejanteDetalle``); el
        filtro decía ``or_r__...``, que no existe y reventaba con
        ``FieldError: Cannot resolve keyword 'or_r'`` en cuanto se consultaba
        el cupo.
        """
        return cantidades_asignadas(OrdenReflejanteDetalle, "orden_r", pedido_ids)

    @staticmethod
    def _cantidades_asignadas_por_linea(pedido):
        """Suma lo ya programado en ORs activas por cada línea (pedido_detalle, talla).

        Retorna ``(por_linea, sin_talla)``; solo considera
        ``OrdenesReflejante.activo=True``.
        """
        return OrdenReflejanteService.cantidades_asignadas_por_pedidos([pedido.pk])

    @staticmethod
    @transaction.atomic
    def save(data, user):
        pedido = data.get("pedido")

        OrdenReflejanteService._validar_contexto(pedido, user)

        sucursal = user.sucursal_default

        if sucursal is None:
            raise ValidationError({"err": "El usuario no tiene una sucursal asignada."})

        detalle_tallas_raw = list(
            OrdenReflejanteService._tallas_reflejante_qs(pedido.id).select_related(
                "pedido_detalle", "talla"
            )
        )

        if not detalle_tallas_raw:
             raise ValidationError({
                "err": "El pedido no tiene detalles con reflejante para generar la orden."
            })

        detalles_override = data.get("detalles_override") or []
        if detalles_override:
            override_map = {
                int(item["pedido_detalle_talla_id"]): float(item["cantidad"])
                for item in detalles_override
            }
            detalle_tallas = [
                next((dt for dt in detalle_tallas_raw if dt.id == pdt_id), None)
                for pdt_id in override_map
            ]
            detalle_tallas = [dt for dt in detalle_tallas if dt is not None]
            for dt in detalle_tallas:
                try:
                    dt.cantidad = override_map[dt.id]
                except AttributeError:
                    pass
        else:
            detalle_tallas = detalle_tallas_raw

        if not detalle_tallas:
            raise ValidationError({
                "err": "No se seleccionaron líneas para generar la orden de reflejante."
            })

        asignado_por_linea, _asignado_sin_talla = (
            OrdenReflejanteService._cantidades_asignadas_por_linea(pedido)
        )
        errores_lineas = []
        for dt in detalle_tallas:
            key = (dt.pedido_detalle_id, getattr(dt.talla, "id", None))
            disponible = float((getattr(dt, "cantidad_pedido_snapshot", None) if hasattr(dt, "cantidad_pedido_snapshot") else None) or 0)
            if not disponible:
                try:
                    from ventas.models import PedidoDetalleTalla
                    original = PedidoDetalleTalla.objects.filter(pk=dt.id).values("cantidad").first()
                    disponible = float((original or {}).get("cantidad") or 0)
                except Exception:
                    disponible = 0.0
            nuevo = float(getattr(dt, "cantidad", None) or 0)
            ya = asignado_por_linea.get(key, 0.0)
            faltante = max(0.0, disponible - ya)
            if nuevo > faltante:
                errores_lineas.append(
                    f"  - talla_id={key[1]} pedido_detalle_id={key[0]}: "
                    f"pedido={disponible}, ya_asignado={ya}, solicitado={nuevo}, "
                    f"disponible_restante={faltante}"
                )
        if errores_lineas:
            raise ValidationError({
                "err": (
                    "No se puede generar la orden de reflejante: una o más líneas "
                    "exceden la cantidad disponible del pedido."
                ),
                "detalles_exceso": errores_lineas,
            })

        es_full_match = (
            not detalles_override
            and {dt.id for dt in detalle_tallas_raw} == {dt.id for dt in detalle_tallas}
            and all(
                float(dt.cantidad or 0) == float(raw.cantidad or 0)
                for dt, raw in zip(
                    sorted(detalle_tallas, key=lambda x: x.id),
                    sorted(detalle_tallas_raw, key=lambda x: x.id),
                )
            )
        )
        if es_full_match:
            existente = OrdenReflejanteService.buscar_existente_full_match(pedido)
            if existente is not None:
                raise OrdenReflejanteDuplicada409(
                    OrdenReflejanteService._payload_duplicada(existente)
                )

        folio_reflejante = generate_or_folio(pedido.empresa_id, pedido.sucursal_id)

        orden_reflejante = crear_orden_con_guardia_duplicado(
            OrdenesReflejante,
            pedido,
            dict(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_reflejante=folio_reflejante,
                usuario_asignado=user,
                prioridad=data.get("prioridad", 1),
                observaciones=data.get("observaciones"),
            ),
            OrdenReflejanteDuplicada409,
            OrdenReflejanteService._payload_duplicada,
        )

        bulk_data = []
        for dt in detalle_tallas:
            cfg = getattr(dt, "reflejante_config", None) or {}
            ubicaciones = cfg.get("ubicaciones") or []
            if isinstance(ubicaciones, list) and ubicaciones:
                primera_ubic = ubicaciones[0] or {}
            else:
                primera_ubic = {}
            posicion_sugerida = (
                cfg.get("posicion")
                or primera_ubic.get("codigo")
                or primera_ubic.get("nombre")
            )
            bulk_data.append(OrdenReflejanteDetalle(
                orden_r=orden_reflejante,
                pedido_detalle=dt.pedido_detalle,
                producto_id=dt.pedido_detalle.producto_id,
                cantidad=float(getattr(dt, "cantidad", None) or 0),
                talla=dt.talla,
                color=getattr(dt.pedido_detalle, "color", None),
                tipo_reflejante=cfg.get("tipo_reflejante") or cfg.get("tipo"),
                posicion=posicion_sugerida,
                metros_reflejante=(
                    cfg.get("metros") or cfg.get("metros_reflejante") or 0
                ),
            ))

        OrdenReflejanteDetalle.objects.bulk_create(bulk_data)
        return orden_reflejante
