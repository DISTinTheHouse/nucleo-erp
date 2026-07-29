from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from wms.utils.decimales import normalizar_decimal
from wms.utils.folios import generate_folio
from wms.models import Packing, PackingDetalle, Picking, PickingDetalle


class PackingService:
    _normalize_quantity = staticmethod(normalizar_decimal)

    @classmethod
    def _packing_scope_queryset(cls, picking):
        return PackingDetalle.objects.filter(packing__picking=picking).exclude(
            packing__estado="CANCELADO"
        ).exclude(estado="CANCELADO")

    @classmethod
    def _historical_packed_map(cls, picking, picking_detalle_ids=None):
        qs = cls._packing_scope_queryset(picking)
        if picking_detalle_ids is not None:
            qs = qs.filter(picking_detalle_id__in=picking_detalle_ids)

        packed_map = defaultdict(lambda: Decimal("0"))
        for row in qs.values("picking_detalle_id").annotate(
            total_empacado=Sum("cantidad_empacada")
        ):
            packed_map[row["picking_detalle_id"]] = cls._normalize_quantity(
                row["total_empacado"]
            )
        return packed_map

    @classmethod
    def onboarding_payload(cls, user, picking_id=None):
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            return {
                "pickings": [],
                "picking": None,
                "packing_detalle": [],
            }

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        sucursal_ids = user.sucursales_permitidas()

        pickings_qs = (
            Picking.objects.filter(empresa=empresa)
            .exclude(estado=Picking.Estado.CANCELADO)
            .select_related(
                "pedido",
                "pedido__cliente",
                "sucursal",
                "operador",
                "almacen",
            )
            .order_by("-created_at", "-id")
        )
        if not es_staff:
            pickings_qs = pickings_qs.filter(sucursal_id__in=sucursal_ids)

        payload = {
            "pickings": [
                {
                    "id": picking.id,
                    "folio": picking.folio,
                    "pedido": picking.pedido_id,
                    "pedido_folio": getattr(picking.pedido, "folio", None),
                    "cliente_nombre": getattr(picking.pedido.cliente, "nombre", None)
                    if getattr(picking, "pedido_id", None)
                    else None,
                    "sucursal": picking.sucursal_id,
                    "sucursal_nombre": getattr(picking.sucursal, "nombre", None),
                    "operador": picking.operador_id,
                    "operador_nombre": picking.operador.get_full_name().strip()
                    or picking.operador.email,
                    "almacen": picking.almacen_id,
                    "almacen_nombre": getattr(picking.almacen, "nombre", None),
                    "estado": picking.estado,
                }
                for picking in pickings_qs[:50]
            ],
            "picking": None,
            "packing_detalle": [],
        }

        if not picking_id:
            return payload

        picking = (
            pickings_qs.filter(pk=picking_id)
            .prefetch_related(
                "picking_detalle",
                "picking_detalle__producto",
                "picking_detalle__producto_variante",
                "picking_detalle__pedido_detalle_talla",
                "picking_detalle__pedido_detalle_talla__variante",
                "picking_detalle__pedido_detalle_talla__variante__talla",
                "picking_detalle__pedido_detalle_talla__variante__color",
                "picking_detalle__ubicacion",
                "picking_detalle__ubicacion__almacen",
            )
            .first()
        )
        if picking is None:
            raise ValidationError({"picking": "Picking no encontrado o sin acceso."})

        detalle_rows = [
            row
            for row in picking.picking_detalle.all().order_by("id")
            if row.estado != PickingDetalle.EstadoLinea.CANCELADA
        ]
        packed_map = cls._historical_packed_map(
            picking,
            picking_detalle_ids=[row.id for row in detalle_rows],
        )

        payload["picking"] = {
            "id": picking.id,
            "folio": picking.folio,
            "pedido": picking.pedido_id,
            "pedido_folio": getattr(picking.pedido, "folio", None),
            "cliente": getattr(picking.pedido, "cliente_id", None),
            "cliente_nombre": getattr(picking.pedido.cliente, "nombre", None)
            if getattr(picking, "pedido_id", None)
            else None,
            "sucursal": picking.sucursal_id,
            "sucursal_nombre": getattr(picking.sucursal, "nombre", None),
            "operador": picking.operador_id,
            "operador_nombre": picking.operador.get_full_name().strip()
            or picking.operador.email,
            "almacen": picking.almacen_id,
            "almacen_nombre": getattr(picking.almacen, "nombre", None),
            "estado": picking.estado,
        }
        payload["packing_detalle"] = [
            {
                "picking_detalle": row.id,
                "pedido_detalle": row.pedido_detalle_id,
                "pedido_detalle_talla": row.pedido_detalle_talla_id,
                "producto": row.producto_id,
                "producto_nombre": getattr(row.producto, "nombre", None),
                "producto_variante": row.producto_variante_id,
                "producto_variante_nombre": getattr(
                    row.producto_variante, "nombre", None
                ),
                "talla": getattr(row.pedido_detalle_talla.variante, "talla_id", None)
                if row.pedido_detalle_talla_id and row.pedido_detalle_talla.variante_id
                else None,
                "talla_nombre": getattr(
                    getattr(row.pedido_detalle_talla.variante, "talla", None),
                    "nombre",
                    None,
                )
                if row.pedido_detalle_talla_id and row.pedido_detalle_talla.variante_id
                else None,
                "color": getattr(row.pedido_detalle_talla.variante, "color_id", None)
                if row.pedido_detalle_talla_id and row.pedido_detalle_talla.variante_id
                else None,
                "color_nombre": getattr(
                    getattr(row.pedido_detalle_talla.variante, "color", None),
                    "nombre",
                    None,
                )
                if row.pedido_detalle_talla_id and row.pedido_detalle_talla.variante_id
                else None,
                "ubicacion": row.ubicacion_id,
                "ubicacion_nombre": str(row.ubicacion) if row.ubicacion_id else None,
                "cantidad_solicitada": str(
                    cls._normalize_quantity(row.cantidad_solicitada)
                ),
                "cantidad_asignada": str(
                    cls._normalize_quantity(row.cantidad_asignada)
                ),
                "cantidad_surtida": str(cls._normalize_quantity(row.cantidad_surtida)),
                "cantidad_ya_empacada": str(packed_map[row.id]),
                "cantidad_pendiente_empacar": str(
                    max(
                        cls._normalize_quantity(row.cantidad_asignada)
                        - packed_map[row.id],
                        Decimal("0"),
                    )
                ),
                "estado": row.estado,
            }
            for row in detalle_rows
        ]
        return payload

    @classmethod
    def _validate_context(cls, picking, user):
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if picking.empresa_id != empresa.pk:
            raise ValidationError("El picking no pertenece a la empresa del usuario.")
        if picking.estado == Picking.Estado.CANCELADO:
            raise ValidationError("No se puede empacar un picking cancelado.")

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        if not es_staff:
            sucursales_permitidas = user.sucursales_permitidas()
            if picking.sucursal_id not in sucursales_permitidas:
                raise ValidationError(
                    "No tiene acceso a la sucursal del picking para generar el packing."
                )

    @classmethod
    def _resolve_requested_rows(cls, picking, requested_rows):
        if not requested_rows:
            raise ValidationError(
                {"packing_detalle": "Debe enviar al menos una línea para empacar."}
            )

        picking_rows = {
            row.id: row
            for row in picking.picking_detalle.exclude(
                estado=PickingDetalle.EstadoLinea.CANCELADA
            ).order_by("id")
        }
        requested_by_picking_detalle = defaultdict(lambda: Decimal("0"))
        normalized_rows = []
        for row in requested_rows:
            picking_detalle_id = row.get("picking_detalle")
            if not picking_detalle_id:
                raise ValidationError(
                    {
                        "packing_detalle": (
                            "Cada línea debe incluir picking_detalle."
                        )
                    }
                )

            cantidad_empacada = cls._normalize_quantity(row.get("cantidad_empacada"))
            if cantidad_empacada <= 0:
                raise ValidationError(
                    {
                        "packing_detalle": (
                            "Cada línea debe incluir una cantidad_empacada mayor a cero."
                        )
                    }
                )

            picking_detalle_id = int(picking_detalle_id)
            requested_by_picking_detalle[picking_detalle_id] += cantidad_empacada
            normalized_rows.append(
                {
                    "picking_detalle_id": picking_detalle_id,
                    "cantidad_empacada": cantidad_empacada,
                    "observaciones": row.get("observaciones"),
                }
            )

        invalid_ids = set(requested_by_picking_detalle.keys()) - set(picking_rows.keys())
        if invalid_ids:
            raise ValidationError(
                {
                    "packing_detalle": (
                        "Los siguientes picking_detalle no pertenecen al picking "
                        f"seleccionado: {sorted(invalid_ids)}"
                    )
                }
            )

        previously_packed_quantities = cls._historical_packed_map(
            picking,
            picking_detalle_ids=list(requested_by_picking_detalle.keys()),
        )
        for picking_detalle_id, cantidad_solicitada in requested_by_picking_detalle.items():
            picking_row = picking_rows[picking_detalle_id]
            cantidad_asignada = cls._normalize_quantity(picking_row.cantidad_asignada)
            cantidad_ya_empacada = previously_packed_quantities[picking_detalle_id]
            cantidad_pendiente = cantidad_asignada - cantidad_ya_empacada

            if cantidad_pendiente <= 0:
                raise ValidationError(
                    {
                        "packing_detalle": (
                            f"El picking_detalle {picking_detalle_id} ya no tiene "
                            "cantidad pendiente por empacar."
                        )
                    }
                )

            if cantidad_solicitada > cantidad_pendiente:
                raise ValidationError(
                    {
                        "packing_detalle": (
                            f"La cantidad empacada para picking_detalle "
                            f"{picking_detalle_id} excede lo pendiente."
                        )
                    }
                )

        return [
            {
                "picking_detalle": picking_rows[row["picking_detalle_id"]],
                "cantidad_empacada": row["cantidad_empacada"],
                "observaciones": row.get("observaciones"),
            }
            for row in normalized_rows
        ]

    @staticmethod
    @transaction.atomic
    def handle_store(data, user):
        picking = (
            Picking.objects.select_for_update()
            .select_related("empresa", "sucursal", "pedido", "operador")
            .get(pk=data.pop("picking").pk)
        )
        packing_detalle = data.pop("packing_detalle")

        PackingService._validate_context(picking, user)
        resolved_rows = PackingService._resolve_requested_rows(picking, packing_detalle)

        folio = generate_folio(picking.empresa, picking.sucursal, "Packing")
        packing = Packing.objects.create(
            folio=folio,
            empresa=picking.empresa,
            sucursal=picking.sucursal,
            pedido=picking.pedido,
            picking=picking,
            operador=picking.operador,
            usuario=user,
            **data,
        )

        bulk_rows = [
            PackingDetalle(
                packing=packing,
                picking_detalle=item["picking_detalle"],
                cantidad_empacada=item["cantidad_empacada"],
                observaciones=item.get("observaciones", None),
            )
            for item in resolved_rows
        ]

        PackingDetalle.objects.bulk_create(bulk_rows)
        return packing
