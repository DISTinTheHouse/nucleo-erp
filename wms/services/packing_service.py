from rest_framework.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from wms.utils.folios import generate_folio
from wms.models import Packing, PackingDetalle


class PackingService:
    @staticmethod
    @transaction.atomic
    def handle_store(data, user):
        picking = data.pop("picking")
        packing_detalle = data.pop("packing_detalle")
        """
        Se contempla que pueden existir 2 o mas packings relacionados al mismo picking
        pero las cantidades a empacar deben respetarse
        """

        # Validar empresa y sucursal
        if (
            picking.empresa != user.empresa
            or picking.sucursal != user.sucursal_default
        ):
            raise ValidationError(
                {"picking": ("El picking no pertenece a la empresa del usuario.")}
            )

        picking_rows = {
            row["id"]: row
            for row in picking.picking_detalle.values("id", "cantidad_asignada")
        }

        # Validar que todos los picking_detalle pertenezcan al picking enviado
        picking_detalle_allowed_ids = set(picking_rows.keys())
        body_picking_detalle_ids = {
            item["picking_detalle"].id for item in packing_detalle
        }

        invalid_ids = body_picking_detalle_ids - picking_detalle_allowed_ids

        if invalid_ids:
            raise ValidationError(
                {
                    "packing_detalle": (
                        f"Los siguientes picking_detalle no pertenecen al picking seleccionado: "
                        f"{sorted(invalid_ids)}"
                    )
                }
            )

        # Obtener cantidades previamente empacadas
        # Solo se consideran packings COMPLETADOS
        previously_packed_quantities = {
            row["picking_detalle"]: row["total_empacado"]
            for row in (
                PackingDetalle.objects.filter(
                    packing__picking=picking,
                    estado="COMPLETADO",
                )
                .values("picking_detalle")
                .annotate(total_empacado=Sum("cantidad_empacada"))
                )
        }

        # Validar cantidades
        for item in packing_detalle:
            picking_detalle_id = item["picking_detalle"].id
            cantidad_empacada = item["cantidad_empacada"]

            cantidad_asignada = picking_rows[picking_detalle_id]["cantidad_asignada"]

            if cantidad_empacada <= 0:
                raise ValidationError({
                    "packing_detalle": (
                        f"La cantidad empacada debe ser mayor a cero para el "
                        f"picking_detalle con id {picking_detalle_id}"
                    )
                })

            previously_packed_quantitie = previously_packed_quantities.get(
                picking_detalle_id, 0
            )

            total_packed = previously_packed_quantitie + cantidad_empacada

            if total_packed > cantidad_asignada:
                raise ValidationError(
                    {
                        "packing_detalle": (
                            f"La cantidad empacada ({cantidad_empacada}) no puede ser mayor "
                            f"que la cantidad asignada ({cantidad_asignada}) para el "
                            f"picking_detalle con id {picking_detalle_id}"
                        )
                    }
                )

        folio = generate_folio(user.empresa, user.sucursal_default, "Packing")
        packing = Packing.objects.create(
            folio=folio,
            empresa=user.empresa,
            sucursal=user.sucursal_default,
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
            for item in packing_detalle
        ]

        PackingDetalle.objects.bulk_create(bulk_rows)
        return packing
