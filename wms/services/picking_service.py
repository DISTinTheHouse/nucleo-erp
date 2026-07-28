from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventarios.models import Almacen
from nucleo.models import SerieFolio
from produccion.models import (
    OrdenBordadoDetalle,
    OrdenCorteMangaDetalle,
    OrdenesBordado,
    OrdenesCorteManga,
    OrdenesReflejante,
    OrdenReflejanteDetalle,
)
from ventas.models import Pedido, PedidoDetalleTalla
from wms.models import Picking, PickingDetalle, PickingOrdenTrabajo
from wms.services.existencia_service import ExistenciaService
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
    def _folio_preview(cls, empresa, sucursal):
        serie_folio = (
            SerieFolio.objects.filter(
                empresa=empresa,
                sucursal=sucursal,
                tipo_documento__iexact="Picking",
                activo=True,
            )
            .order_by("id_serie_folio")
            .only("prefijo", "sufijo", "folio_actual", "longitud_consecutivo")
            .first()
        )
        if not serie_folio:
            return None
        actual = int(getattr(serie_folio, "folio_actual", 0) or 0)
        siguiente = actual + 1
        prefijo = (getattr(serie_folio, "prefijo", "") or "")
        sufijo = (getattr(serie_folio, "sufijo", "") or "")
        longitud = int(getattr(serie_folio, "longitud_consecutivo", 0) or 0)
        consecutivo = str(siguiente).zfill(longitud) if longitud else str(siguiente)
        if sufijo:
            return f"{prefijo}{consecutivo}-{sufijo}"
        return f"{prefijo}{consecutivo}"

    @staticmethod
    def _parse_almacen_id(raw):
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @classmethod
    def onboarding_payload(
        cls,
        user,
        pedido_id=None,
        almacen_origen_id=None,
        almacen_destino_id=None,
    ):
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            return {
                "pedidos": [],
                "operadores": [],
                "almacenes": [],
                "almacen_origen": None,
                "almacen_destino": None,
                "header": {
                    "fecha_picking_sugerida": None,
                    "folio_sugerido_preview": None,
                },
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

        almacen_origen_id = cls._parse_almacen_id(almacen_origen_id)
        almacen_destino_id = cls._parse_almacen_id(almacen_destino_id)

        almacen_origen = None
        almacen_destino = None
        if almacen_origen_id:
            almacen_origen = almacenes_qs.filter(pk=almacen_origen_id).first()
        if almacen_destino_id:
            almacen_destino = almacenes_qs.filter(pk=almacen_destino_id).first()

        header = {
            "fecha_picking_sugerida": None,
            "folio_sugerido_preview": None,
        }

        payload = {
            "pedidos": pedidos_payload,
            "operadores": operadores_payload,
            "almacenes": almacenes_payload,
            "almacen_origen": (
                {
                    "id": almacen_origen.pk,
                    "codigo": almacen_origen.codigo,
                    "nombre": almacen_origen.nombre,
                    "sucursal": almacen_origen.sucursal_id,
                }
                if almacen_origen
                else None
            ),
            "almacen_destino": (
                {
                    "id": almacen_destino.pk,
                    "codigo": almacen_destino.codigo,
                    "nombre": almacen_destino.nombre,
                    "sucursal": almacen_destino.sucursal_id,
                }
                if almacen_destino
                else None
            ),
            "header": header,
            "pedido": None,
            "picking_detalle": [],
        }

        if not pedido_id:
            return payload

        pedido = pedido_qs.filter(pk=pedido_id).first()
        if pedido is None:
            raise ValidationError({"pedido": "Pedido no encontrado o sin acceso."})

        fecha_picking_sugerida = timezone.now()
        folio_sugerido_preview = cls._folio_preview(pedido.empresa, pedido.sucursal)
        header["fecha_picking_sugerida"] = fecha_picking_sugerida.isoformat()
        header["folio_sugerido_preview"] = folio_sugerido_preview

        if almacen_origen is None and payload["almacen_origen"] is None:
            almacen_origen_candidato = (
                Almacen.objects.filter(
                    empresa_id=pedido.empresa_id,
                    sucursal_id=pedido.sucursal_id,
                )
                .order_by("pk")
                .first()
            )
            if almacen_origen_candidato:
                payload["almacen_origen"] = {
                    "id": almacen_origen_candidato.pk,
                    "codigo": almacen_origen_candidato.codigo,
                    "nombre": almacen_origen_candidato.nombre,
                    "sucursal": almacen_origen_candidato.sucursal_id,
                }
                almacen_origen = almacen_origen_candidato

        if almacen_destino is None and payload["almacen_destino"] is None:
            almacen_apartados = cls._resolve_apartados_safe(pedido.empresa_id, pedido.sucursal_id)
            if almacen_apartados:
                payload["almacen_destino"] = {
                    "id": almacen_apartados.pk,
                    "codigo": almacen_apartados.codigo,
                    "nombre": almacen_apartados.nombre,
                    "sucursal": almacen_apartados.sucursal_id,
                }
                almacen_destino = almacen_apartados

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

        existencia_by_talla = {}
        if almacen_origen:
            existencia_by_talla = ExistenciaService.get_existencia_batch(almacen_origen, tallas)

        detalle_payload = []
        for talla in tallas:
            cantidad_pedida = cls._normalize_quantity(talla.cantidad)
            cantidad_asignada = asignado_map[talla.id]
            cantidad_surtida = surtido_map[talla.id]
            cantidad_pendiente = cantidad_pedida - cantidad_asignada
            if cantidad_pendiente < Decimal("0"):
                cantidad_pendiente = Decimal("0")

            existencia_row = existencia_by_talla.get(
                talla.id, {"fisica": Decimal("0"), "reservada": Decimal("0"), "disponible": Decimal("0")}
            )
            existencia_fisica = cls._normalize_quantity(existencia_row.get("fisica"))
            existencia_reservada = cls._normalize_quantity(existencia_row.get("reservada"))
            existencia_disponible = cls._normalize_quantity(existencia_row.get("disponible"))
            maximo_picking_permitido = min(cantidad_pendiente, existencia_disponible)
            if maximo_picking_permitido < Decimal("0"):
                maximo_picking_permitido = Decimal("0")

            requiere_bordado = bool(getattr(talla, "lleva_bordado", False))
            requiere_reflejante = bool(getattr(talla, "lleva_reflejante", False))
            requiere_corte_manga = bool(getattr(talla, "lleva_corte_manga", False))

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
                    "existencia_fisica": str(existencia_fisica),
                    "existencia_reservada": str(existencia_reservada),
                    "existencia_disponible": str(existencia_disponible),
                    "maximo_picking_permitido": str(maximo_picking_permitido),
                    "requiere_bordado": requiere_bordado,
                    "requiere_reflejante": requiere_reflejante,
                    "requiere_corte_manga": requiere_corte_manga,
                    "bordado_config": getattr(talla, "bordado_config", None),
                    "reflejante_config": getattr(talla, "reflejante_config", None),
                    "corte_manga_config": getattr(talla, "corte_manga_config", None),
                }
            )

        payload["header"] = header
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

    @staticmethod
    def _resolve_apartados_safe(empresa_id, sucursal_id):
        return (
            Almacen.objects.filter(
                nombre__iexact="APARTADOS",
                empresa_id=empresa_id,
                sucursal_id=sucursal_id,
            )
            .order_by("pk")
            .first()
        )

    @classmethod
    def _resolve_requested_items(
        cls, pedido, requested_rows, almacen_origen=None
    ):
        if not requested_rows:
            raise ValidationError(
                {"picking_detalle": "Debe enviar al menos una línea para surtir."}
            )

        quantity_by_talla = defaultdict(lambda: Decimal("0"))
        extras_by_talla = {}
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
            key = int(talla_id)
            quantity_by_talla[key] += cantidad
            extras_by_talla.setdefault(key, {
                "generar_orden_bordado": bool(row.get("generar_orden_bordado", False)),
                "generar_orden_reflejante": bool(row.get("generar_orden_reflejante", False)),
                "generar_orden_corte_manga": bool(row.get("generar_orden_corte_manga", False)),
                "observaciones": row.get("observaciones") or "",
            })

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

        existencia_by_talla = {}
        if almacen_origen:
            existencia_by_talla = ExistenciaService.get_existencia_batch(almacen_origen, tallas)

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

            existencia_row = existencia_by_talla.get(
                talla.id, {"fisica": Decimal("0"), "reservada": Decimal("0"), "disponible": Decimal("0")}
            )
            existencia_disponible = cls._normalize_quantity(existencia_row.get("disponible"))

            cantidad_solicitada = quantity_by_talla[talla.id]
            if cantidad_solicitada > cantidad_pendiente:
                raise ValidationError(
                    {
                        "picking_detalle": (
                            f"La cantidad solicitada para la talla {talla.id} excede lo pendiente "
                            f"(solicitada={cantidad_solicitada}, pendiente={cantidad_pendiente})."
                        )
                    }
                )
            if almacen_origen and cantidad_solicitada > existencia_disponible:
                raise ValidationError(
                    {
                        "picking_detalle": (
                            f"La cantidad solicitada para la talla {talla.id} excede la existencia disponible "
                            f"en el almacén origen (solicitada={cantidad_solicitada}, "
                            f"disponible={existencia_disponible}, pendiente_pedido={cantidad_pendiente})."
                        )
                    }
                )

            extras = extras_by_talla.get(talla.id, {})
            if extras.get("generar_orden_bordado") and not getattr(talla, "lleva_bordado", False):
                raise ValidationError(
                    {
                        "picking_detalle": (
                            f"La línea de talla {talla.id} no requiere bordado; no puede marcar generar_orden_bordado."
                        )
                    }
                )
            if extras.get("generar_orden_reflejante") and not getattr(talla, "lleva_reflejante", False):
                raise ValidationError(
                    {
                        "picking_detalle": (
                            f"La línea de talla {talla.id} no requiere reflejante; no puede marcar generar_orden_reflejante."
                        )
                    }
                )
            if extras.get("generar_orden_corte_manga") and not getattr(talla, "lleva_corte_manga", False):
                raise ValidationError(
                    {
                        "picking_detalle": (
                            f"La línea de talla {talla.id} no requiere corte de manga; no puede marcar generar_orden_corte_manga."
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
                    "existencia_disponible": existencia_disponible,
                    "generar_orden_bordado": extras.get("generar_orden_bordado", False),
                    "generar_orden_reflejante": extras.get("generar_orden_reflejante", False),
                    "generar_orden_corte_manga": extras.get("generar_orden_corte_manga", False),
                    "observaciones": extras.get("observaciones") or "",
                }
            )

        return requested_items

    @staticmethod
    def _validate_context(pedido, almacen, almacen_destino, operador, user):
        empresa = getattr(user, "empresa", None)

        if empresa is None:
            raise ValidationError("El usuario no tiene una empresa asignada.")
        if pedido.empresa_id != empresa.pk:
            raise ValidationError("El pedido no pertenece a la empresa del usuario.")
        if almacen.empresa_id and almacen.empresa_id != pedido.empresa_id:
            raise ValidationError("El almacén origen no pertenece a la empresa del pedido.")
        if almacen.sucursal_id and almacen.sucursal_id != pedido.sucursal_id:
            raise ValidationError("El almacén origen no pertenece a la sucursal del pedido.")
        if almacen_destino:
            if almacen_destino.empresa_id and almacen_destino.empresa_id != pedido.empresa_id:
                raise ValidationError("El almacén destino no pertenece a la empresa del pedido.")
            if almacen_destino.sucursal_id and almacen_destino.sucursal_id != pedido.sucursal_id:
                raise ValidationError("El almacén destino no pertenece a la sucursal del pedido.")
            if almacen.pk == almacen_destino.pk:
                raise ValidationError("El almacén origen y destino no pueden ser iguales.")
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
                    "No tiene acceso a la sucursal del almacén origen seleccionado."
                )
            if almacen_destino and almacen_destino.sucursal_id and almacen_destino.sucursal_id not in sucursales_permitidas:
                raise ValidationError(
                    "No tiene acceso a la sucursal del almacén destino seleccionado."
                )

    @staticmethod
    def _resolve_apartados(pedido):
        almacen_apartados = (
            Almacen.objects.filter(
                nombre="APARTADOS",
                empresa_id=pedido.empresa_id,
                sucursal_id=pedido.sucursal_id,
            )
            .order_by("pk")
            .first()
        )
        if not almacen_apartados:
            raise ValidationError(
                "No existe el almacén APARTADOS para la empresa y sucursal del pedido."
            )
        return almacen_apartados

    @classmethod
    def _generar_ordenes_trabajo(cls, picking, requested_items, user):
        bordado_items = [it for it in requested_items if it["generar_orden_bordado"]]
        reflejante_items = [it for it in requested_items if it["generar_orden_reflejante"]]
        corte_items = [it for it in requested_items if it["generar_orden_corte_manga"]]

        resultado = []
        enlaces = []

        if bordado_items:
            folio = generate_folio(picking.empresa, picking.sucursal, "Bordado")
            orden = OrdenesBordado.objects.create(
                empresa=picking.empresa,
                sucursal=picking.sucursal,
                pedido=picking.pedido,
                folio_bordado=folio,
                prioridad=1,
                fecha_inicio=timezone.now(),
                usuario_asignado=getattr(user, "pk", None) or None,
                observaciones=f"Generada automáticamente desde picking {picking.folio}.",
                activo=True,
            )
            detalles = []
            for item in bordado_items:
                talla = item["talla"]
                variante = getattr(talla, "variante", None)
                detalles.append(
                    OrdenBordadoDetalle(
                        ob=orden,
                        pedido_detalle=talla.pedido_detalle,
                        producto=talla.pedido_detalle.producto,
                        cantidad=float(item["cantidad"]),
                        posicion_bordado=(
                            (getattr(talla, "bordado_config", None) or {}).get("posicion")
                            if isinstance(getattr(talla, "bordado_config", None), dict)
                            else None
                        ),
                        colores_hilo=int(
                            (getattr(talla, "bordado_config", None) or {}).get("colores_hilo", 0)
                            if isinstance(getattr(talla, "bordado_config", None), dict)
                            else 0
                        ),
                        puntadas=int(
                            (getattr(talla, "bordado_config", None) or {}).get("puntadas", 0)
                            if isinstance(getattr(talla, "bordado_config", None), dict)
                            else 0
                        ),
                        talla=getattr(variante, "talla", None),
                        color=getattr(variante, "color", None),
                    )
                )
            OrdenBordadoDetalle.objects.bulk_create(detalles)
            resultado.append({"tipo": "BORDADO", "id": orden.pk, "folio": orden.folio_bordado})
            enlaces.append(PickingOrdenTrabajo(
                picking=picking,
                tipo_orden=PickingOrdenTrabajo.TipoOrden.BORDADO,
                orden_bordado=orden,
            ))

        if reflejante_items:
            folio = generate_folio(picking.empresa, picking.sucursal, "Reflejante")
            orden = OrdenesReflejante.objects.create(
                empresa=picking.empresa,
                sucursal=picking.sucursal,
                pedido=picking.pedido,
                folio_reflejante=folio,
                prioridad=1,
                fecha_inicio=timezone.now(),
                usuario_asignado=getattr(user, "pk", None) or None,
                observaciones=f"Generada automáticamente desde picking {picking.folio}.",
                activo=True,
            )
            detalles = []
            for item in reflejante_items:
                talla = item["talla"]
                variante = getattr(talla, "variante", None)
                cfg = getattr(talla, "reflejante_config", None) if isinstance(getattr(talla, "reflejante_config", None), dict) else {}
                detalles.append(
                    OrdenReflejanteDetalle(
                        orden_r=orden,
                        pedido_detalle=talla.pedido_detalle,
                        producto=talla.pedido_detalle.producto,
                        cantidad=float(item["cantidad"]),
                        tipo_reflejante=cfg.get("tipo_reflejante") if isinstance(cfg, dict) else None,
                        posicion=cfg.get("posicion") if isinstance(cfg, dict) else None,
                        metros=float((cfg.get("metros") if isinstance(cfg, dict) else cfg) or 0.0),
                        talla=getattr(variante, "talla", None),
                        color=getattr(variante, "color", None),
                    )
                )
            OrdenReflejanteDetalle.objects.bulk_create(detalles)
            resultado.append({"tipo": "REFLEJANTE", "id": orden.pk, "folio": orden.folio_reflejante})
            enlaces.append(PickingOrdenTrabajo(
                picking=picking,
                tipo_orden=PickingOrdenTrabajo.TipoOrden.REFLEJANTE,
                orden_reflejante=orden,
            ))

        if corte_items:
            folio = generate_folio(picking.empresa, picking.sucursal, "CorteManga")
            orden = OrdenesCorteManga.objects.create(
                empresa=picking.empresa,
                sucursal=picking.sucursal,
                pedido=picking.pedido,
                folio_ocm=folio,
                prioridad=1,
                fecha_inicio=timezone.now(),
                usuario_asignado=getattr(user, "pk", None) or None,
                observaciones=f"Generada automáticamente desde picking {picking.folio}.",
                activo=True,
            )
            detalles = []
            for item in corte_items:
                talla = item["talla"]
                variante = getattr(talla, "variante", None)
                cfg = getattr(talla, "corte_manga_config", None) if isinstance(getattr(talla, "corte_manga_config", None), dict) else {}
                detalles.append(
                    OrdenCorteMangaDetalle(
                        ocm=orden,
                        pedido_detalle=talla.pedido_detalle,
                        producto=talla.pedido_detalle.producto,
                        cantidad=float(item["cantidad"]),
                        talla=getattr(variante, "talla", None),
                        color=getattr(variante, "color", None),
                        configuracion=cfg or None,
                    )
                )
            OrdenCorteMangaDetalle.objects.bulk_create(detalles)
            resultado.append({"tipo": "CORTE_MANGA", "id": orden.pk, "folio": orden.folio_ocm})
            enlaces.append(PickingOrdenTrabajo(
                picking=picking,
                tipo_orden=PickingOrdenTrabajo.TipoOrden.CORTE_MANGA,
                orden_corte_manga=orden,
            ))

        if enlaces:
            PickingOrdenTrabajo.objects.bulk_create(enlaces)
        return resultado

    @classmethod
    @transaction.atomic
    def handle_store(cls, data, user):
        pedido = data.pop("pedido")
        almacen = data.pop("almacen")
        almacen_destino = data.pop("almacen_destino", None)
        operador = data.pop("operador")
        requested_rows = data.pop("picking_detalle")

        if almacen_destino is None:
            almacen_destino = cls._resolve_apartados(pedido)

        cls._validate_context(pedido, almacen, almacen_destino, operador, user)
        requested_items = cls._resolve_requested_items(
            pedido, requested_rows, almacen_origen=almacen
        )

        reservas = ReservaInventarioService.create_for_picking(
            pedido=pedido,
            almacen=almacen,
            requested_items=requested_items,
            user=user,
        )

        transferencia_data = {
            "almacen_origen": almacen,
            "almacen_destino": almacen_destino,
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
            almacen_destino=almacen_destino,
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
                    observaciones=item.get("observaciones") or None,
                )
            )

        PickingDetalle.objects.bulk_create(picking_rows)
        picking.total_lineas_completas = lineas_completas
        picking.save(update_fields=["total_lineas_completas", "updated_at"])
        ReservaInventarioService.apply_to_picking(reservas, picking, transferencia)

        ordenes_trabajo = cls._generar_ordenes_trabajo(picking, requested_items, user)
        return picking, ordenes_trabajo
