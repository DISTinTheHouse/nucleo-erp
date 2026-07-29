from collections import defaultdict

from django.db import transaction
from rest_framework.exceptions import ValidationError

from logistica.models import Envio
from wms.models import Despacho, DespachoDetalle, Packing


class DespachoService:
    @classmethod
    def _despachado_map(cls, packing, packing_detalle_ids=None):
        qs = DespachoDetalle.objects.filter(despacho__packing=packing)
        if packing_detalle_ids is not None:
            qs = qs.filter(packing_detalle_id__in=packing_detalle_ids)

        map_ids = defaultdict(lambda: False)
        for row in qs.values_list("packing_detalle_id", flat=True):
            map_ids[row] = True
        return map_ids

    @classmethod
    def onboarding_payload(cls, user, packing_id=None):
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            return {
                "packings": [],
                "envios": [],
                "packing": None,
                "despacho_detalle": [],
            }

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        sucursal_ids = user.sucursales_permitidas()

        packings_qs = (
            Packing.objects.filter(empresa=empresa)
            .exclude(estado="CANCELADO")
            .select_related(
                "pedido",
                "pedido__cliente",
                "sucursal",
                "picking",
                "picking__almacen",
                "operador",
            )
            .order_by("-created_at", "-id")
        )
        if not es_staff:
            packings_qs = packings_qs.filter(sucursal_id__in=sucursal_ids)

        payload = {
            "packings": [
                {
                    "id": packing.id,
                    "folio": packing.folio,
                    "pedido": packing.pedido_id,
                    "pedido_folio": getattr(packing.pedido, "folio", None),
                    "cliente_nombre": getattr(packing.pedido.cliente, "nombre", None)
                    if getattr(packing, "pedido_id", None)
                    else None,
                    "sucursal": packing.sucursal_id,
                    "sucursal_nombre": getattr(packing.sucursal, "nombre", None),
                    "picking": packing.picking_id,
                    "picking_folio": getattr(packing.picking, "folio", None),
                    "almacen": getattr(packing.picking, "almacen_id", None),
                    "almacen_nombre": getattr(getattr(packing.picking, "almacen", None), "nombre", None),
                    "estado": packing.estado,
                }
                for packing in packings_qs[:50]
            ],
            "envios": [],
            "packing": None,
            "despacho_detalle": [],
        }

        if not packing_id:
            return payload

        packing = (
            packings_qs.filter(pk=packing_id)
            .prefetch_related(
                "packing_detalle",
                "packing_detalle__caja",
                "packing_detalle__picking_detalle",
                "packing_detalle__picking_detalle__producto",
                "packing_detalle__picking_detalle__producto_variante",
                "packing_detalle__picking_detalle__pedido_detalle_talla",
                "packing_detalle__picking_detalle__pedido_detalle_talla__variante",
                "packing_detalle__picking_detalle__pedido_detalle_talla__variante__talla",
                "packing_detalle__picking_detalle__pedido_detalle_talla__variante__color",
                "packing_detalle__picking_detalle__ubicacion",
                "packing_detalle__picking_detalle__ubicacion__almacen",
            )
            .first()
        )
        if packing is None:
            raise ValidationError({"packing": "Packing no encontrado o sin acceso."})

        envios_qs = (
            Envio.objects.filter(
                empresa=empresa,
                pedido_id=packing.pedido_id,
                sucursal_id=packing.sucursal_id,
            )
            .select_related("transportista")
            .order_by("-id")
        )
        payload["envios"] = [
            {
                "id": envio.id,
                "pedido": envio.pedido_id,
                "transportista": envio.transportista_id,
                "transportista_nombre": getattr(envio.transportista, "nombre", None),
            }
            for envio in envios_qs[:50]
        ]

        payload["packing"] = {
            "id": packing.id,
            "folio": packing.folio,
            "pedido": packing.pedido_id,
            "pedido_folio": getattr(packing.pedido, "folio", None),
            "cliente": getattr(packing.pedido, "cliente_id", None),
            "cliente_nombre": getattr(packing.pedido.cliente, "nombre", None)
            if getattr(packing, "pedido_id", None)
            else None,
            "sucursal": packing.sucursal_id,
            "sucursal_nombre": getattr(packing.sucursal, "nombre", None),
            "picking": packing.picking_id,
            "picking_folio": getattr(packing.picking, "folio", None),
            "almacen": getattr(packing.picking, "almacen_id", None),
            "almacen_nombre": getattr(getattr(packing.picking, "almacen", None), "nombre", None),
            "estado": packing.estado,
        }

        rows = list(packing.packing_detalle.all().order_by("id"))
        already_dispatched = cls._despachado_map(
            packing, packing_detalle_ids=[row.id for row in rows]
        )
        payload["despacho_detalle"] = [
            {
                "packing_detalle": row.id,
                "picking_detalle": row.picking_detalle_id,
                "pedido_detalle": getattr(row.picking_detalle, "pedido_detalle_id", None),
                "pedido_detalle_talla": getattr(row.picking_detalle, "pedido_detalle_talla_id", None),
                "producto": getattr(row.picking_detalle, "producto_id", None),
                "producto_nombre": getattr(getattr(row.picking_detalle, "producto", None), "nombre", None),
                "producto_variante": getattr(row.picking_detalle, "producto_variante_id", None),
                "producto_variante_nombre": getattr(
                    getattr(row.picking_detalle, "producto_variante", None),
                    "nombre",
                    None,
                ),
                "talla": getattr(
                    getattr(getattr(row.picking_detalle, "pedido_detalle_talla", None), "variante", None),
                    "talla_id",
                    None,
                ),
                "talla_nombre": getattr(
                    getattr(
                        getattr(
                            getattr(row.picking_detalle, "pedido_detalle_talla", None),
                            "variante",
                            None,
                        ),
                        "talla",
                        None,
                    ),
                    "nombre",
                    None,
                ),
                "color": getattr(
                    getattr(getattr(row.picking_detalle, "pedido_detalle_talla", None), "variante", None),
                    "color_id",
                    None,
                ),
                "color_nombre": getattr(
                    getattr(
                        getattr(
                            getattr(row.picking_detalle, "pedido_detalle_talla", None),
                            "variante",
                            None,
                        ),
                        "color",
                        None,
                    ),
                    "nombre",
                    None,
                ),
                "ubicacion": getattr(row.picking_detalle, "ubicacion_id", None),
                "ubicacion_nombre": str(row.picking_detalle.ubicacion)
                if getattr(row.picking_detalle, "ubicacion_id", None)
                else None,
                "caja": row.caja_id,
                "caja_numero": getattr(getattr(row, "caja", None), "numero", None),
                "cantidad_empacada": str(row.cantidad_empacada),
                "estado": row.estado,
                "ya_despachado": bool(already_dispatched[row.id]),
                "disponible_para_despacho": not bool(already_dispatched[row.id]),
            }
            for row in rows
        ]
        return payload

    @classmethod
    def _validate_context(cls, packing, envio, user):
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if packing.empresa_id != empresa.pk:
            raise ValidationError("El packing no pertenece a la empresa del usuario.")
        if packing.estado == "CANCELADO":
            raise ValidationError("No se puede despachar un packing cancelado.")

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        if not es_staff:
            sucursales_permitidas = user.sucursales_permitidas()
            if packing.sucursal_id not in sucursales_permitidas:
                raise ValidationError(
                    "No tiene acceso a la sucursal del packing para generar el despacho."
                )

        if envio is None:
            return

        if envio.empresa_id != empresa.pk:
            raise ValidationError("El envío no pertenece a la empresa del usuario.")
        if packing.pedido_id != envio.pedido_id:
            raise ValidationError("El envío no corresponde al pedido del packing.")
        if packing.sucursal_id != envio.sucursal_id:
            raise ValidationError("El envío no corresponde a la sucursal del packing.")

    @classmethod
    def _resolve_requested_rows(cls, packing, requested_rows):
        if not requested_rows:
            raise ValidationError(
                {"despacho_detalle": "Debe enviar al menos una línea para despachar."}
            )

        packing_rows = {
            row.id: row for row in packing.packing_detalle.all().order_by("id")
        }
        requested_ids = []
        for row in requested_rows:
            packing_detalle_id = row.get("packing_detalle")
            if not packing_detalle_id:
                raise ValidationError(
                    {
                        "despacho_detalle": (
                            "Cada línea debe incluir packing_detalle."
                        )
                    }
                )
            requested_ids.append(int(packing_detalle_id))

        invalid_ids = set(requested_ids) - set(packing_rows.keys())
        if invalid_ids:
            raise ValidationError(
                {
                    "despacho_detalle": (
                        "Los siguientes packing_detalle no pertenecen al packing "
                        f"seleccionado: {sorted(invalid_ids)}"
                    )
                }
            )

        already_dispatched = cls._despachado_map(packing, packing_detalle_ids=requested_ids)
        for packing_detalle_id in requested_ids:
            if already_dispatched[packing_detalle_id]:
                raise ValidationError(
                    {
                        "despacho_detalle": (
                            f"El packing_detalle {packing_detalle_id} ya fue despachado."
                        )
                    }
                )

        return [packing_rows[row_id] for row_id in requested_ids]

    @staticmethod
    @transaction.atomic
    def handle_store(data, user):
        packing = (
            Packing.objects.select_for_update()
            .select_related("empresa", "sucursal", "pedido", "picking", "picking__almacen")
            .get(pk=data.pop("packing").pk)
        )
        envio_input = data.pop("envio", None)
        envio = None
        if envio_input is not None:
            envio = Envio.objects.select_for_update().select_related("transportista").get(
                pk=envio_input.pk
            )
        despacho_detalle = data.pop("despacho_detalle")

        DespachoService._validate_context(packing, envio, user)
        resolved_rows = DespachoService._resolve_requested_rows(packing, despacho_detalle)

        despacho = Despacho.objects.create(
            packing=packing,
            envio=envio,
        )
        DespachoDetalle.objects.bulk_create(
            [
                DespachoDetalle(
                    despacho=despacho,
                    packing_detalle=row,
                )
                for row in resolved_rows
            ]
        )
        return despacho
