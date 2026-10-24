from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from logistica.models import Envio
from ventas.models import Pedido, PedidoDetalleTalla
from ventas.services.sku_produccion_service import TALLAS_DE_LINEA_NO_MUESTRA_Q, linea_no_muestra_q
from wms.models import Despacho, DespachoDetalle, Packing, PackingDetalle, PickingDetalle

# Línea de packing cuya línea de pedido no es muestra (mismo criterio que
# ``TALLAS_DE_LINEA_NO_MUESTRA_Q``).
PACKING_DETALLE_NO_MUESTRA_Q = linea_no_muestra_q("picking_detalle__pedido_detalle__")


def _piezas(cantidad):
    """``Decimal`` de piezas sin ceros de relleno (``188.0000`` -> ``188``)."""
    return f"{cantidad.normalize():f}"


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
    def _packings_totalmente_despachados(cls, base_qs):
        """IDs de packings de ``base_qs`` donde ya no queda ningún renglón por despachar.

        Un ``packing_detalle`` activo cuenta como pendiente si NO tiene
        ningún ``DespachoDetalle`` ligado; si todos los renglones activos de
        un packing ya lo tienen, el packing queda fuera de candidatos.
        """
        activos_por_packing = defaultdict(set)
        for pd_id, packing_id in (
            PackingDetalle.objects.filter(packing__in=base_qs)
            .exclude(estado="CANCELADO")
            .values_list("id", "packing_id")
        ):
            activos_por_packing[packing_id].add(pd_id)

        despachados_ids = set(
            DespachoDetalle.objects.filter(
                packing_detalle__packing__in=base_qs
            ).values_list("packing_detalle_id", flat=True)
        )

        return {
            packing_id
            for packing_id, pd_ids in activos_por_packing.items()
            if pd_ids and pd_ids.issubset(despachados_ids)
        }

    @classmethod
    def onboarding_payload(cls, user, packing_id=None):
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            return {
                "packings": [],
                "envios": [],
                "packing": None,
                "despacho_detalle": [],
                "embarque": None,
            }

        es_staff = getattr(user, "is_superuser", False) or getattr(
            user, "is_admin_empresa", False
        )
        sucursal_ids = user.sucursales_permitidas()

        packings_qs = (
            Packing.objects.filter(empresa=empresa)
            .exclude(estado="CANCELADO")
        )
        if not es_staff:
            packings_qs = packings_qs.filter(sucursal_id__in=sucursal_ids)

        # Un packing ya despachado por completo no tiene nada más que ofrecer
        # aquí: fuera de la lista de candidatos.
        packings_qs = (
            packings_qs.exclude(pk__in=cls._packings_totalmente_despachados(packings_qs))
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
            "embarque": None,
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

        # Las líneas canceladas no se ofrecen para despachar (EC-574).
        rows = list(packing.packing_detalle.exclude(estado="CANCELADO").order_by("id"))
        already_dispatched = cls._despachado_map(
            packing, packing_detalle_ids=[row.id for row in rows]
        )
        # Sin embarque parcial y con el pedido sin empacar completo, la regla O1
        # rechazaría cualquier despacho: las líneas no se ofrecen y ``embarque``
        # dice por qué (EC-574).
        embarque = cls._resumen_embarque(packing.pedido)
        payload["embarque"] = embarque
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
                "disponible_para_despacho": not bool(already_dispatched[row.id])
                and embarque["despachable"],
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
    def _resumen_embarque(cls, pedido):
        """Piezas del pedido contra lo empacado vivo, con el criterio de la regla
        O1 (EC-574 / WM-02): cuentan packings y líneas vivos de líneas de picking
        no canceladas, y las muestras (con su propio flujo) quedan fuera de ambos
        lados. ``despachable`` dice si O1 deja despachar."""
        total = Decimal(
            PedidoDetalleTalla.objects.filter(pedido_detalle__pedido_id=pedido.pk)
            .filter(TALLAS_DE_LINEA_NO_MUESTRA_Q)
            .aggregate(total=Sum("cantidad"))["total"]
            or 0
        )
        empacado = (
            PackingDetalle.objects.filter(packing__pedido_id=pedido.pk)
            .exclude(packing__estado="CANCELADO")
            .exclude(estado="CANCELADO")
            .exclude(picking_detalle__estado=PickingDetalle.EstadoLinea.CANCELADA)
            .filter(PACKING_DETALLE_NO_MUESTRA_Q)
            .aggregate(total=Sum("cantidad_empacada"))["total"]
            or Decimal("0")
        )
        faltan = max(total - empacado, Decimal("0"))
        despachable = bool(pedido.embarque_parcial) or faltan == 0
        return {
            "embarque_parcial": bool(pedido.embarque_parcial),
            "total_piezas_pedido": _piezas(total),
            "piezas_empacadas": _piezas(empacado),
            "piezas_por_empacar": _piezas(faltan),
            "despachable": despachable,
            "motivo": None
            if despachable
            else (
                "El pedido no permite embarque parcial: faltan "
                f"{_piezas(faltan)} piezas por empacar de las {_piezas(total)} del pedido."
            ),
        }

    @classmethod
    def _validar_embarque_parcial(cls, packing, rows):
        """Con ``pedido.embarque_parcial`` apagado solo se despacha cuando todo lo
        pedido ya está empacado, y cada despacho lleva todas las líneas vivas
        pendientes de su packing: un despacho por packing (EC-574 / WM-02,
        regla O1)."""
        pedido = packing.pedido
        if pedido.embarque_parcial:
            return
        embarque = cls._resumen_embarque(pedido)
        if not embarque["despachable"]:
            raise ValidationError({"despacho_detalle": embarque["motivo"]})

        vivas = set(
            packing.packing_detalle.exclude(estado="CANCELADO")
            .exclude(despacho_detalle__isnull=False)
            .values_list("id", flat=True)
        )
        faltantes = vivas - {row.pk for row in rows}
        if faltantes:
            raise ValidationError(
                {
                    "despacho_detalle": (
                        "El pedido no permite embarque parcial: el despacho debe llevar "
                        f"todas las líneas del packing (faltan: {sorted(faltantes)})."
                    )
                }
            )

    @classmethod
    def _resolve_requested_rows(cls, packing, requested_rows):
        if not requested_rows:
            raise ValidationError(
                {"despacho_detalle": "Debe enviar al menos una línea para despachar."}
            )

        packing_rows = {
            row.id: row for row in packing.packing_detalle.all().order_by("id")
        }
        cancelados = {row_id for row_id, row in packing_rows.items() if row.estado == "CANCELADO"}
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

        cancelados_pedidos = cancelados.intersection(requested_ids)
        if cancelados_pedidos:
            raise ValidationError(
                {
                    "despacho_detalle": (
                        "Los siguientes packing_detalle están cancelados y no se "
                        f"pueden despachar: {sorted(cancelados_pedidos)}"
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
        packing_input = data.pop("packing")
        envio_input = data.pop("envio", None)
        despacho_detalle = data.pop("despacho_detalle")
        guia = data.pop("guia", "") or ""

        # Tenencia antes de bloquear nada; después, el orden de bloqueo común a
        # packing y despacho (EC-574): Pedido -> Picking -> líneas de picking ->
        # Packing -> Envio (el despacho no toca el picking), siempre
        # ``of=("self",)`` para no bloquear de paso empresa ni sucursal. El
        # pedido serializa los despachos de sus packings mientras se valida O1.
        DespachoService._validate_context(packing_input, envio_input, user)
        Pedido.objects.select_for_update(of=("self",)).filter(pk=packing_input.pedido_id).first()
        packing = (
            Packing.objects.select_for_update(of=("self",))
            .select_related("empresa", "sucursal", "pedido", "picking", "picking__almacen")
            .get(pk=packing_input.pk)
        )
        envio = None
        if envio_input is not None:
            envio = (
                Envio.objects.select_for_update(of=("self",))
                .select_related("transportista")
                .get(pk=envio_input.pk)
            )

        DespachoService._validate_context(packing, envio, user)
        resolved_rows = DespachoService._resolve_requested_rows(packing, despacho_detalle)
        DespachoService._validar_embarque_parcial(packing, resolved_rows)

        despacho = Despacho.objects.create(
            packing=packing,
            envio=envio,
            guia=guia,
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
