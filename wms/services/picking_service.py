from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from inventarios.models import Almacen
from ventas.models import Pedido, PedidoDetalleTalla
from wms.models import Picking, PickingDetalle
from wms.services.reserva_service import ReservaInventarioService
from wms.services.transferencia_service import TransferenciaService
from wms.utils.folios import generate_folio


class PickingService:
    @staticmethod
    def _normalize_quantity(value):
        return Decimal(str(value or "0"))

    @classmethod
    def _picking_scope_queryset(cls, pedido):
        return PickingDetalle.objects.filter(
            pedido_detalle__pedido=pedido,
            pedido_detalle_talla__isnull=False,
        ).exclude(
            picking__estado=Picking.Estado.CANCELADO,
        ).exclude(
            estado=PickingDetalle.EstadoLinea.CANCELADA,
        )

    @classmethod
    def _historical_maps(cls, pedido, talla_ids=None):
        qs = cls._picking_scope_queryset(pedido)
        if talla_ids is not None:
            qs = qs.filter(pedido_detalle_talla_id__in=talla_ids)

        asignado_map = defaultdict(lambda: Decimal("0"))
        surtido_map = defaultdict(lambda: Decimal("0"))
        for row in qs.values("pedido_detalle_talla_id").annotate(
            total_asignado=Sum("cantidad_asignada"),
            total_surtido=Sum("cantidad_surtida"),
        ):
            talla_id = row["pedido_detalle_talla_id"]
            asignado_map[talla_id] = cls._normalize_quantity(row["total_asignado"])
            surtido_map[talla_id] = cls._normalize_quantity(row["total_surtido"])
        return asignado_map, surtido_map

    @classmethod
    def onboarding_payload(cls, user, pedido_id=None):
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            return {
                "pedidos": [],
                "operadores": [],
                "almacenes": [],
                "pedido": None,
                "picking_detalle": [],
            }

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        sucursal_ids = list(user.sucursales.values_list("pk", flat=True))
        if user.sucursal_default_id and user.sucursal_default_id not in sucursal_ids:
            sucursal_ids.append(user.sucursal_default_id)

        pedido_qs = (
            Pedido.objects.filter(
                empresa=empresa,
                activo=True,
                estatus__in=[3, 4],
            )
            .select_related("cliente", "sucursal")
            .order_by("-id")
        )
        if not es_staff:
            pedido_qs = pedido_qs.filter(sucursal_id__in=sucursal_ids)

        pedidos_payload = [
            {
                "id": pedido.id,
                "folio": pedido.folio,
                "cliente": pedido.cliente_id,
                "cliente_nombre": getattr(pedido.cliente, "nombre", None),
                "sucursal": pedido.sucursal_id,
                "sucursal_nombre": getattr(pedido.sucursal, "nombre", None),
            }
            for pedido in pedido_qs[:50]
        ]

        operadores_qs = (
            user.__class__.objects.filter(empresa=empresa, is_active=True)
            .order_by("first_name", "last_name", "email")
            .only("id", "first_name", "last_name", "email")
        )
        if not es_staff:
            operadores_qs = operadores_qs.filter(sucursal_default_id__in=sucursal_ids)

        operadores_payload = [
            {
                "id": operador.id,
                "nombre": operador.get_full_name().strip() or operador.email,
            }
            for operador in operadores_qs[:100]
        ]

        almacenes_qs = Almacen.objects.filter(empresa=empresa).order_by("codigo")
        if not es_staff:
            almacenes_qs = almacenes_qs.filter(sucursal_id__in=sucursal_ids)
        almacenes_payload = [
            {
                "id": almacen.pk,
                "codigo": almacen.codigo,
                "nombre": almacen.nombre,
                "sucursal": almacen.sucursal_id,
            }
            for almacen in almacenes_qs[:100]
        ]

        payload = {
            "pedidos": pedidos_payload,
            "operadores": operadores_payload,
            "almacenes": almacenes_payload,
            "pedido": None,
            "picking_detalle": [],
        }

        if not pedido_id:
            return payload

        pedido = pedido_qs.filter(pk=pedido_id).first()
        if pedido is None:
            raise ValidationError({"pedido": "Pedido no encontrado o sin acceso."})

        tallas = list(
            PedidoDetalleTalla.objects.filter(pedido_detalle__pedido=pedido)
            .select_related(
                "pedido_detalle__producto",
                "variante",
                "variante__talla",
                "variante__color",
            )
            .order_by("pedido_detalle_id", "id")
        )
        talla_ids = [t.id for t in tallas]
        asignado_map, surtido_map = cls._historical_maps(pedido, talla_ids=talla_ids)

        detalle_payload = []
        for talla in tallas:
            cantidad_pedida = cls._normalize_quantity(talla.cantidad)
            cantidad_asignada = asignado_map[talla.id]
            cantidad_surtida = surtido_map[talla.id]
            cantidad_pendiente = cantidad_pedida - cantidad_asignada
            if cantidad_pendiente < Decimal("0"):
                cantidad_pendiente = Decimal("0")

            detalle_payload.append(
                {
                    "pedido_detalle": talla.pedido_detalle_id,
                    "pedido_detalle_talla": talla.id,
                    "producto": talla.pedido_detalle.producto_id,
                    "producto_nombre": talla.pedido_detalle.producto.nombre,
                    "producto_variante": talla.variante_id,
                    "producto_variante_nombre": str(talla.variante)
                    if talla.variante_id
                    else None,
                    "talla": getattr(talla.variante, "talla_id", None),
                    "talla_nombre": getattr(getattr(talla.variante, "talla", None), "nombre", None),
                    "color": getattr(talla.variante, "color_id", None),
                    "color_nombre": getattr(getattr(talla.variante, "color", None), "nombre", None),
                    "cantidad_pedida": str(cantidad_pedida),
                    "cantidad_ya_asignada": str(cantidad_asignada),
                    "cantidad_ya_surtida": str(cantidad_surtida),
                    "cantidad_pendiente": str(cantidad_pendiente),
                }
            )

        payload["pedido"] = {
            "id": pedido.id,
            "folio": pedido.folio,
            "cliente": pedido.cliente_id,
            "cliente_nombre": getattr(pedido.cliente, "nombre", None),
            "sucursal": pedido.sucursal_id,
            "sucursal_nombre": getattr(pedido.sucursal, "nombre", None),
        }
        payload["picking_detalle"] = detalle_payload
        return payload

    @classmethod
    def _resolve_requested_items(cls, pedido, requested_rows):
        if not requested_rows:
            raise ValidationError(
                {"picking_detalle": "Debe enviar al menos una línea para surtir."}
            )

        quantity_by_talla = defaultdict(lambda: Decimal("0"))
        for row in requested_rows:
            talla_id = row.get("pedido_detalle_talla")
            if not talla_id:
                raise ValidationError(
                    {"picking_detalle": "Cada línea debe incluir pedido_detalle_talla."}
                )
            cantidad = cls._normalize_quantity(row.get("cantidad_asignada"))
            if cantidad <= 0:
                raise ValidationError(
                    {
                        "picking_detalle": (
                            "Cada línea debe incluir una cantidad_asignada mayor a cero."
                        )
                    }
                )
            quantity_by_talla[int(talla_id)] += cantidad

        tallas = list(
            PedidoDetalleTalla.objects.filter(
                pedido_detalle__pedido=pedido,
                pk__in=list(quantity_by_talla.keys()),
            )
            .select_related(
                "pedido_detalle__producto",
                "variante",
                "variante__talla",
                "variante__color",
            )
            .order_by("pedido_detalle_id", "id")
        )
        if len(tallas) != len(quantity_by_talla):
            raise ValidationError(
                {
                    "picking_detalle": (
                        "Una o más líneas no pertenecen al pedido indicado."
                    )
                }
            )

        asignado_map, surtido_map = cls._historical_maps(
            pedido, talla_ids=list(quantity_by_talla.keys())
        )

        requested_items = []
        for talla in tallas:
            cantidad_pedida = cls._normalize_quantity(talla.cantidad)
            cantidad_asignada = asignado_map[talla.id]
            cantidad_surtida = surtido_map[talla.id]
            cantidad_pendiente = cantidad_pedida - cantidad_asignada
            if cantidad_pendiente <= Decimal("0"):
                raise ValidationError(
                    {
                        "picking_detalle": (
                            f"La línea de talla {talla.id} ya no tiene cantidad pendiente."
                        )
                    }
                )

            cantidad_solicitada = quantity_by_talla[talla.id]
            if cantidad_solicitada > cantidad_pendiente:
                raise ValidationError(
                    {
                        "picking_detalle": (
                            f"La cantidad solicitada para la talla {talla.id} excede lo pendiente."
                        )
                    }
                )

            requested_items.append(
                {
                    "talla": talla,
                    "cantidad": cantidad_solicitada,
                    "cantidad_pedida": cantidad_pedida,
                    "cantidad_asignada_historica": cantidad_asignada,
                    "cantidad_surtida_historica": cantidad_surtida,
                    "cantidad_pendiente": cantidad_pendiente,
                }
            )

        return requested_items

    @staticmethod
    def _validate_context(pedido, almacen, operador, user):
        empresa = getattr(user, "empresa", None)

        if empresa is None:
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if pedido.empresa_id != empresa.pk:
            raise ValidationError("El pedido no pertenece a la empresa del usuario.")
        if almacen.empresa_id and almacen.empresa_id != pedido.empresa_id:
            raise ValidationError("El almacén no pertenece a la empresa del pedido.")
        if almacen.sucursal_id and almacen.sucursal_id != pedido.sucursal_id:
            raise ValidationError("El almacén no pertenece a la sucursal del pedido.")
        if getattr(operador, "empresa_id", None) != pedido.empresa_id:
            raise ValidationError("El operador no pertenece a la empresa del pedido.")
        if not getattr(operador, "is_active", False):
            raise ValidationError("El operador no está activo.")

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        if not es_staff:
            sucursales_permitidas = set(user.sucursales.values_list("pk", flat=True))
            if user.sucursal_default_id:
                sucursales_permitidas.add(user.sucursal_default_id)
            if pedido.sucursal_id not in sucursales_permitidas:
                raise ValidationError(
                    "No tiene acceso a la sucursal del pedido para generar el picking."
                )
            if almacen.sucursal_id and almacen.sucursal_id not in sucursales_permitidas:
                raise ValidationError(
                    "No tiene acceso a la sucursal del almacén seleccionado."
                )

    @staticmethod
    def _resolve_apartados(pedido):
        almacen_apartados = (
            Almacen.objects.filter(
                nombre="APARTADOS",
                empresa_id=pedido.empresa_id,
                sucursal_id=pedido.sucursal_id,
            )
            .order_by("id_almacen")
            .first()
        )
        if not almacen_apartados:
            raise ValidationError(
                "No existe el almacén APARTADOS para la empresa y sucursal del pedido."
            )
        return almacen_apartados

    @classmethod
    @transaction.atomic
    def handle_store(cls, data, user):
        pedido = data.pop("pedido")
        almacen = data.pop("almacen")
        operador = data.pop("operador")
        requested_rows = data.pop("picking_detalle")

        cls._validate_context(pedido, almacen, operador, user)
        requested_items = cls._resolve_requested_items(pedido, requested_rows)
        almacen_apartados = cls._resolve_apartados(pedido)

        reservas = ReservaInventarioService.create_for_picking(
            pedido=pedido,
            almacen=almacen,
            requested_items=requested_items,
            user=user,
        )

        transferencia_data = {
            "almacen_origen": almacen,
            "almacen_destino": almacen_apartados,
            "observaciones": "Generada desde picking",
            "transferencia_detalle": [
                {
                    "producto": item["talla"].pedido_detalle.producto,
                    "producto_variante": item["talla"].variante,
                    "cantidad": item["cantidad"],
                }
                for item in requested_items
            ],
        }

        transferencia = TransferenciaService.handle_store(transferencia_data, user)

        folio = generate_folio(pedido.empresa, pedido.sucursal, "Picking")
        picking = Picking.objects.create(
            folio=folio,
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            operador=operador,
            almacen=almacen,
            usuario=user,
            total_lineas=len(requested_items),
            **data,
        )

        picking_rows = []
        lineas_completas = 0
        for item in requested_items:
            talla = item["talla"]
            cantidad = item["cantidad"]
            pendiente = item["cantidad_pendiente"]
            if cantidad == pendiente:
                lineas_completas += 1

            picking_rows.append(
                PickingDetalle(
                    picking=picking,
                    pedido_detalle=talla.pedido_detalle,
                    pedido_detalle_talla=talla,
                    producto=talla.pedido_detalle.producto,
                    producto_variante=talla.variante,
                    cantidad_solicitada=cantidad,
                    cantidad_asignada=cantidad,
                    operador=operador,
                )
            )

        PickingDetalle.objects.bulk_create(picking_rows)
        picking.total_lineas_completas = lineas_completas
        picking.save(update_fields=["total_lineas_completas", "updated_at"])
        ReservaInventarioService.apply_to_picking(reservas, picking, transferencia)
        return picking
