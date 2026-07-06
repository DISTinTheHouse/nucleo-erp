import logging
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from datetime import timedelta
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from django.conf import settings

from auditoria.models import AuditoriaEvento
from inventarios.models import (
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    TipoMovimiento,
)
from ventas.models import (
    Cotizacion,
    CotizacionDetalle,
    CotizacionDetalleTalla,
    CotizacionServicioExtra,
    Pedido,
    PedidoDetalle,
    PedidoDetalleTalla,
    PedidoServicioExtra,
)

from ventas.api.serializers import (
    CotizacionSerializer,
    CotizacionDashboardItemSerializer,
    CotizacionDetalleSerializer,
    CotizacionDetalleWithTallasSerializer,
    CotizacionFullSerializer,
    PedidoSerializer,
    PedidoDetalleSerializer,
    PedidoDetalleTallaSerializer,
    PedidoDetalleWithTallasSerializer,
    CotizacionOnboardingCreateSerializer,
)

from nucleo.models import SerieFolio, Empresa
from produccion.models import (
    OrdenesBordado,
    OrdenBordadoDetalle,
    OrdenesReflejante,
    OrdenReflejanteDetalle,
    OrdenProduccion,
    OrdenesCorteManga,
    OrdenCorteMangaDetalle,
)

from ventas.utils.helpers import _save_cotizacion_detalle, _save_servicios_extras

logger = logging.getLogger(__name__)
QTY_PRECISION = Decimal("0.0001")


class CotizacionViewSet(viewsets.ModelViewSet):
    queryset = Cotizacion.objects.all()
    serializer_class = CotizacionSerializer
    http_method_names = ["get", "post", "patch"]

    def get_serializer_class(self):
        if getattr(self, "action", None) == "list":
            return CotizacionDashboardItemSerializer
        if getattr(self, "action", None) == "retrieve":
            return CotizacionFullSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related("cliente", "sucursal", "moneda", "vendedor")
        )
        if getattr(user, "is_superuser", False):
            return self._apply_filters(qs)
        empresa = getattr(user, "empresa", None)
        if empresa:
            qs = qs.filter(empresa=empresa)
            if not getattr(user, "is_admin_empresa", False):
                qs = qs.filter(vendedor=user)
            return self._apply_filters(qs)
        return qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        serializer.save(empresa=empresa, vendedor=user)

    def perform_update(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        cotizacion = self.get_object()
        if (
            not getattr(user, "is_superuser", False)
            and empresa
            and cotizacion.empresa_id != empresa.pk
        ):
            raise ValidationError({"cotizacion": "No tienes acceso a esta cotización."})
        if not getattr(user, "is_superuser", False) and not getattr(
            user, "is_admin_empresa", False
        ):
            if cotizacion.vendedor_id and cotizacion.vendedor_id != getattr(
                user, "id", None
            ):
                raise ValidationError(
                    {"cotizacion": "No tienes acceso a esta cotización."}
                )
        edit_minutes = int(getattr(settings, "COTIZACION_EDIT_WINDOW_MINUTES", 30))
        edit_minutes = max(1, edit_minutes)

        if cotizacion.estatus == 3 and cotizacion.autorizada_at:
            limite = cotizacion.autorizada_at + timedelta(minutes=edit_minutes)
            if (
                timezone.now() > limite
                and not getattr(user, "is_superuser", False)
                and not getattr(user, "is_admin_empresa", False)
            ):
                raise ValidationError(
                    {
                        "cotizacion": "La cotización ya no está dentro del periodo permitido para edición."
                    }
                )
            cotizacion.estatus = 5
            cotizacion.cambios_solicitados_at = timezone.now()

        serializer.save()
        if cotizacion.estatus == 5:
            cotizacion.save(update_fields=["estatus", "cambios_solicitados_at"])

    def _apply_filters(self, qs):
        if getattr(self, "action", None) == "retrieve":
            detalles_qs = CotizacionDetalle.objects.select_related(
                "producto"
            ).prefetch_related(
                Prefetch(
                    "tallas",
                    queryset=CotizacionDetalleTalla.objects.select_related("talla"),
                )
            )
            qs = qs.prefetch_related(
                Prefetch("cotizaciondetalle", queryset=detalles_qs)
            )

        if getattr(self, "action", None) == "list":
            pedido_qs = Pedido.objects.filter(cotizacion_id=OuterRef("pk")).order_by(
                "-id"
            )
            qs = qs.annotate(
                pedido_id=Subquery(pedido_qs.values("id")[:1]),
                pedido_folio=Subquery(pedido_qs.values("folio")[:1]),
                piezas=Coalesce(Sum("cotizaciondetalle__tallas__cantidad"), 0),
            )

        estatus = self.request.query_params.get("estatus")
        if estatus:
            try:
                estatus_list = [
                    int(x) for x in str(estatus).split(",") if str(x).strip()
                ]
                qs = qs.filter(estatus__in=estatus_list)
            except Exception:
                raise ValidationError(
                    {"estatus": "Filtro inválido. Usa números separados por coma."}
                )

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            q_filter = (
                Q(oc__icontains=q)
                | Q(cliente__razon_social__icontains=q)
                | Q(cliente__nombre__icontains=q)
                | Q(cliente__rfc__icontains=q)
            )
            if q.isdigit():
                q_filter = q_filter | Q(id=int(q))
            qs = qs.filter(q_filter)

        ordering = (self.request.query_params.get("ordering") or "-created_at").strip()
        allowed = {"id", "created_at", "updated_at", "gran_total", "estatus"}
        ordering_fields = []
        for part in ordering.split(","):
            part = part.strip()
            if not part:
                continue
            desc = part.startswith("-")
            field = part[1:] if desc else part
            if field in allowed:
                ordering_fields.append(part)
        if ordering_fields:
            qs = qs.order_by(*ordering_fields)
        else:
            qs = qs.order_by("-created_at")

        return qs

    def _asignar_folio_pedido(self, pedido, empresa):
        if pedido.folio:
            return
        serie_folio = (
            SerieFolio.objects.select_for_update()
            .filter(
                empresa=empresa,
                sucursal=pedido.sucursal,
                tipo_documento__iexact="PEDIDO",
                activo=True,
            )
            .order_by("id_serie_folio")
            .first()
        )
        if not serie_folio:
            raise ValidationError(
                {
                    "serie_folio": "No hay una Serie/Folio activa configurada para tipo_documento='Pedido' en esta sucursal."
                }
            )
        try:
            folio_formateado, nuevo_consecutivo, anio_actual = (
                serie_folio.get_siguiente_folio()
            )
        except Exception:
            raise ValidationError({"folio": "No se pudo generar el folio del pedido."})
        serie_folio.folio_actual = nuevo_consecutivo
        serie_folio.ultimo_anio = anio_actual
        serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])
        pedido.serie_folio = serie_folio
        pedido.folio = folio_formateado
        pedido.folio_consecutivo = nuevo_consecutivo
        pedido.save(update_fields=["serie_folio", "folio", "folio_consecutivo"])

    def _snapshot_facturacion_pedido(self, pedido):
        try:
            cl = pedido.cliente
        except Exception:
            cl = None
        if not cl:
            return
        updates = {}
        if not pedido.cliente_razon_social:
            updates["cliente_razon_social"] = cl.razon_social
        if not pedido.cliente_nombre:
            updates["cliente_nombre"] = cl.nombre
        if not pedido.cliente_rfc:
            updates["cliente_rfc"] = cl.rfc
        if not pedido.cliente_regimen_fiscal_id and getattr(
            cl, "sat_regimen_fiscal_id", None
        ):
            updates["cliente_regimen_fiscal_id"] = cl.sat_regimen_fiscal_id
        if not pedido.cliente_direccion_fiscal:
            updates["cliente_direccion_fiscal"] = cl.direccion_fiscal
        if not pedido.cliente_colonia:
            updates["cliente_colonia"] = cl.colonia
        if not pedido.cliente_codigo_postal:
            updates["cliente_codigo_postal"] = cl.codigo_postal
        if not pedido.cliente_ciudad:
            updates["cliente_ciudad"] = cl.ciudad
        if not pedido.cliente_estado:
            updates["cliente_estado"] = cl.estado
        if not pedido.cliente_giro_empresarial:
            updates["cliente_giro_empresarial"] = cl.giro_empresarial
        if updates:
            for k, v in updates.items():
                setattr(pedido, k, v)
            pedido.save(update_fields=list(updates.keys()))

    def _snapshot_cotizacion(self, cotizacion_obj):
        detalles_qs = (
            CotizacionDetalle.objects.filter(cotizacion=cotizacion_obj)
            .prefetch_related("tallas")
            .order_by("id")
        )
        servicios_extras_qs = CotizacionServicioExtra.objects.filter(
            cotizacion=cotizacion_obj
        ).order_by("id")
        return {
            "cotizacion": CotizacionSerializer(cotizacion_obj).data,
            "detalles": CotizacionDetalleWithTallasSerializer(
                detalles_qs, many=True
            ).data,
            "servicios_extras": list(
                servicios_extras_qs.values(
                    "nombre", "monto", "cantidad", "visible_en_factura"
                )
            ),
        }

    def _to_decimal_inventory(self, value):
        return Decimal(str(value or 0)).quantize(
            QTY_PRECISION, rounding=ROUND_HALF_UP
        )

    def _build_inventory_plan_from_rows(self, rows, producto_attr, variante_attr, cantidad_attr):
        plan = {}
        for row in rows:
            producto = getattr(row, producto_attr)
            variante = getattr(row, variante_attr)
            cantidad = self._to_decimal_inventory(getattr(row, cantidad_attr))
            key = (
                "variante",
                variante.pk,
            ) if variante else (
                "producto",
                producto.pk,
            )

            if key not in plan:
                plan[key] = {
                    "producto": producto,
                    "producto_id": producto.pk,
                    "producto_variante_id": getattr(variante, "pk", None),
                    "cantidad": Decimal("0.0000"),
                }
            plan[key]["cantidad"] += cantidad

        return list(plan.values())

    def _build_pedido_inventory_plan(self, pedido):
        detalles_talla = (
            PedidoDetalleTalla.objects.filter(
                pedido_detalle__pedido=pedido,
                cantidad__gt=0,
            )
            .select_related("pedido_detalle__producto", "variante")
            .order_by("id")
        )
        plan = {}
        for row in detalles_talla:
            producto = row.pedido_detalle.producto
            variante = row.variante
            cantidad = self._to_decimal_inventory(row.cantidad)
            key = (
                "variante",
                variante.pk,
            ) if variante else (
                "producto",
                producto.pk,
            )

            if key not in plan:
                plan[key] = {
                    "producto": producto,
                    "producto_id": producto.pk,
                    "producto_variante_id": getattr(variante, "pk", None),
                    "cantidad": Decimal("0.0000"),
                }
            plan[key]["cantidad"] += cantidad

        return list(plan.values())

    def _build_cotizacion_inventory_plan(self, cotizacion):
        detalles_talla = (
            CotizacionDetalleTalla.objects.filter(
                cotizacion_detalle__cotizacion=cotizacion,
                cantidad__gt=0,
            )
            .select_related("cotizacion_detalle__producto", "variante")
            .order_by("id")
        )
        plan = {}
        for row in detalles_talla:
            producto = row.cotizacion_detalle.producto
            variante = row.variante
            cantidad = self._to_decimal_inventory(row.cantidad)
            key = (
                "variante",
                variante.pk,
            ) if variante else (
                "producto",
                producto.pk,
            )

            if key not in plan:
                plan[key] = {
                    "producto": producto,
                    "producto_id": producto.pk,
                    "producto_variante_id": getattr(variante, "pk", None),
                    "cantidad": Decimal("0.0000"),
                }
            plan[key]["cantidad"] += cantidad

        return list(plan.values())

    def _compute_inventory_delta(self, current_plan, target_plan):
        current_map = {
            (item["producto_id"], item["producto_variante_id"]): item
            for item in current_plan
        }
        target_map = {
            (item["producto_id"], item["producto_variante_id"]): item
            for item in target_plan
        }
        keys = set(current_map.keys()) | set(target_map.keys())

        salidas = []
        entradas = []
        for key in keys:
            current_item = current_map.get(key)
            target_item = target_map.get(key)
            producto = (
                (target_item or current_item)["producto"]
            )
            delta = self._to_decimal_inventory(
                (target_item or {}).get("cantidad", 0)
            ) - self._to_decimal_inventory(
                (current_item or {}).get("cantidad", 0)
            )
            if delta > 0:
                salidas.append(
                    {
                        "producto": producto,
                        "producto_id": key[0],
                        "producto_variante_id": key[1],
                        "cantidad": delta,
                    }
                )
            elif delta < 0:
                entradas.append(
                    {
                        "producto": producto,
                        "producto_id": key[0],
                        "producto_variante_id": key[1],
                        "cantidad": abs(delta),
                    }
                )
        return salidas, entradas

    def _update_existencia_quantity(self, existencia, nueva_cantidad):
        nueva_cantidad = self._to_decimal_inventory(nueva_cantidad)
        existencia.cantidad = nueva_cantidad
        existencia.stock = max(int(nueva_cantidad), 0)
        existencia.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])

    def _get_request_meta(self, request=None):
        if request is None:
            return None, None
        ip = request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get(
            "REMOTE_ADDR"
        )
        user_agent = request.META.get("HTTP_USER_AGENT")
        return ip, user_agent

    def _get_pedido_inventory_location_balance(self, pedido, producto_id, producto_variante_id):
        balance = {}
        eventos = AuditoriaEvento.objects.filter(
            modulo="inventarios",
            tabla="existencias",
            id_registro=str(pedido.pk),
        ).order_by("id_evento")

        for evento in eventos:
            payload = evento.despues_json or {}
            for item in payload.get("items", []):
                if item.get("producto_id") != producto_id:
                    continue
                if item.get("producto_variante_id") != producto_variante_id:
                    continue
                key = (
                    item.get("almacen_id"),
                    item.get("ubicacion_id"),
                )
                balance.setdefault(key, Decimal("0.0000"))
                balance[key] += -self._to_decimal_inventory(item.get("delta", 0))

        return [
            {
                "almacen_id": almacen_id,
                "ubicacion_id": ubicacion_id,
                "cantidad_disponible_retorno": cantidad,
            }
            for (almacen_id, ubicacion_id), cantidad in sorted(
                balance.items(),
                key=lambda entry: (-entry[1], entry[0][0] or 0, entry[0][1] or 0),
            )
            if cantidad > 0
        ]


    def _discount_existencias_pedido(self, plan, empresa, sucursal):
        consumos = []

        for item in plan:
            producto = item["producto"]
            producto_variante_id = item["producto_variante_id"]
            cantidad_requerida = item["cantidad"].quantize(
                QTY_PRECISION, rounding=ROUND_HALF_UP
            )

            existencias = (
                Existencia.objects.select_for_update()
                .filter(
                    almacen__empresa_id=empresa.pk,
                    almacen__sucursal_id=sucursal.pk,
                )
                .order_by("-cantidad", "id")
            )
            if producto_variante_id:
                existencias = existencias.filter(
                    producto_variante_id=producto_variante_id
                )
            else:
                existencias = existencias.filter(
                    producto_id=producto.pk,
                    producto_variante__isnull=True,
                )
            existencias = list(existencias)

            disponible = sum(
                (
                    self._to_decimal_inventory(existencia.cantidad)
                    for existencia in existencias
                ),
                Decimal("0.0000"),
            )
            if disponible < cantidad_requerida:
                raise ValidationError(
                    {
                        "inventario": (
                            f"Existencia insuficiente para {producto.nombre}. "
                            f"Requerido: {cantidad_requerida}, disponible: {disponible}."
                        )
                    }
                )

            restante = cantidad_requerida
            for existencia in existencias:
                if restante <= 0:
                    break

                cantidad_actual = self._to_decimal_inventory(existencia.cantidad)
                if cantidad_actual <= 0:
                    continue

                cantidad_consumida = min(cantidad_actual, restante).quantize(
                    QTY_PRECISION, rounding=ROUND_HALF_UP
                )
                cantidad_nueva = (cantidad_actual - cantidad_consumida).quantize(
                    QTY_PRECISION, rounding=ROUND_HALF_UP
                )

                self._update_existencia_quantity(existencia, cantidad_nueva)

                consumos.append(
                    {
                        "producto": producto,
                        "producto_id": producto.pk,
                        "producto_variante_id": existencia.producto_variante_id,
                        "existencia_id": existencia.pk,
                        "almacen_id": existencia.almacen_id,
                        "ubicacion_id": existencia.ubicacion_id,
                        "cantidad_before": cantidad_actual,
                        "cantidad_after": cantidad_nueva,
                        "cantidad_movimiento": cantidad_consumida,
                        "cantidad_consumida": cantidad_consumida,
                        "delta": -cantidad_consumida,
                    }
                )
                restante -= cantidad_consumida

        return consumos

    def _restore_existencias_pedido(self, pedido, plan):
        entradas = []

        for item in plan:
            producto = item["producto"]
            producto_id = item["producto_id"]
            producto_variante_id = item["producto_variante_id"]
            restante = self._to_decimal_inventory(item["cantidad"])
            targets = self._get_pedido_inventory_location_balance(
                pedido=pedido,
                producto_id=producto_id,
                producto_variante_id=producto_variante_id,
            )

            if not targets:
                raise ValidationError(
                    {
                        "inventario": (
                            f"No se pudo determinar la ubicación para devolver existencias de {producto.nombre}."
                        )
                    }
                )

            for target in targets:
                if restante <= 0:
                    break

                capacidad = self._to_decimal_inventory(
                    target["cantidad_disponible_retorno"]
                )
                if capacidad <= 0:
                    continue

                cantidad_retorno = min(capacidad, restante).quantize(
                    QTY_PRECISION, rounding=ROUND_HALF_UP
                )
                existencia = (
                    Existencia.objects.select_for_update()
                    .filter(
                        producto_id=producto_id,
                        producto_variante_id=producto_variante_id,
                        almacen_id=target["almacen_id"],
                        ubicacion_id=target["ubicacion_id"],
                    )
                    .first()
                )
                if existencia is None:
                    existencia = Existencia.objects.create(
                        producto_id=producto_id,
                        producto_variante_id=producto_variante_id,
                        almacen_id=target["almacen_id"],
                        ubicacion_id=target["ubicacion_id"],
                        cantidad=Decimal("0.0000"),
                        stock=0,
                    )

                cantidad_actual = self._to_decimal_inventory(existencia.cantidad)
                cantidad_nueva = (cantidad_actual + cantidad_retorno).quantize(
                    QTY_PRECISION, rounding=ROUND_HALF_UP
                )
                self._update_existencia_quantity(existencia, cantidad_nueva)

                entradas.append(
                    {
                        "producto": producto,
                        "producto_id": producto_id,
                        "producto_variante_id": producto_variante_id,
                        "existencia_id": existencia.pk,
                        "almacen_id": existencia.almacen_id,
                        "ubicacion_id": existencia.ubicacion_id,
                        "cantidad_before": cantidad_actual,
                        "cantidad_after": cantidad_nueva,
                        "cantidad_movimiento": cantidad_retorno,
                        "delta": cantidad_retorno,
                    }
                )
                restante -= cantidad_retorno

            if restante > 0:
                raise ValidationError(
                    {
                        "inventario": (
                            f"No se pudo devolver completamente el inventario de {producto.nombre}. "
                            f"Pendiente por regresar: {restante}."
                        )
                    }
                )

        return entradas

    def _registrar_movimiento_inventario_pedido(
        self,
        pedido,
        user,
        items,
        tipo_movimiento,
        observaciones,
    ):
        movimiento = MovimientoInventario.objects.create(
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
            pedido=pedido,
            entrega_id=None,
            devolucion_id=None,
            ajuste_inventario_id=None,
            tipo_movimiento=tipo_movimiento,
            usuario=user,
            observaciones=observaciones,
            recepcion_id=None,
            transferencia_id=None,
            op_id=None,
        )

        MovimientoInventarioDetalle.objects.bulk_create(
            [
                MovimientoInventarioDetalle(
                    movimiento_inventario=movimiento,
                    producto_id=item["producto_id"],
                    ubicacion_origen_id=item["ubicacion_id"],
                    ubicacion_destino_id=None,
                    lote_id=None,
                    serie_id=None,
                    cantidad=item["cantidad_movimiento"],
                    costo_unitario=Decimal("0"),
                )
                for item in items
            ]
        )
        return movimiento

    def _registrar_auditoria_inventario_pedido(
        self,
        pedido,
        user,
        items,
        accion,
        request=None,
    ):
        items = [
            {
                "producto_id": item["producto_id"],
                "producto_variante_id": item["producto_variante_id"],
                "almacen_id": item["almacen_id"],
                "ubicacion_id": item["ubicacion_id"],
                "cantidad_before": str(item["cantidad_before"]),
                "cantidad_after": str(item["cantidad_after"]),
                "delta": str(item["delta"]),
            }
            for item in items
        ]

        ip, user_agent = self._get_request_meta(request)

        return AuditoriaEvento.objects.create(
            empresa=pedido.empresa,
            usuario=user if getattr(user, "pk", None) else None,
            modulo="inventarios",
            accion=accion,
            tabla="existencias",
            id_registro=str(pedido.pk),
            antes_json={"items": items, "pedido_id": pedido.pk, "folio": pedido.folio},
            despues_json={
                "empresa_id": pedido.empresa_id,
                "sucursal_id": pedido.sucursal_id,
                "pedido_id": pedido.pk,
                "folio": pedido.folio,
                "items": items,
            },
            ip=ip,
            user_agent=user_agent,
        )

    def _descontar_existencias_pedido(self, pedido, user, request=None):
        plan = self._build_pedido_inventory_plan(pedido)
        if not plan:
            return None, None

        consumos = self._discount_existencias_pedido(
            plan=plan,
            empresa=pedido.empresa,
            sucursal=pedido.sucursal,
        )
        movimiento = self._registrar_movimiento_inventario_pedido(
            pedido=pedido,
            user=user,
            items=consumos,
            tipo_movimiento=TipoMovimiento.SALIDA,
            observaciones=(
                f"Descuento automático por autorización de pedido {pedido.folio or pedido.pk}"
            ),
        )
        auditoria = self._registrar_auditoria_inventario_pedido(
            pedido=pedido,
            user=user,
            items=consumos,
            accion="SALIDA",
            request=request,
        )
        return movimiento, auditoria

    def _ajustar_existencias_cambios_pedido(self, cotizacion, pedido, user, request=None):
        plan_actual = self._build_pedido_inventory_plan(pedido)
        plan_objetivo = self._build_cotizacion_inventory_plan(cotizacion)
        salidas, entradas = self._compute_inventory_delta(plan_actual, plan_objetivo)

        resultados = {"salida": None, "entrada": None}
        if entradas:
            items_entrada = self._restore_existencias_pedido(pedido=pedido, plan=entradas)
            resultados["entrada"] = (
                self._registrar_movimiento_inventario_pedido(
                    pedido=pedido,
                    user=user,
                    items=items_entrada,
                    tipo_movimiento=TipoMovimiento.ENTRADA,
                    observaciones=(
                        f"Reintegro automático por cambios aceptados en pedido {pedido.folio or pedido.pk}"
                    ),
                ),
                self._registrar_auditoria_inventario_pedido(
                    pedido=pedido,
                    user=user,
                    items=items_entrada,
                    accion="ENTRADA",
                    request=request,
                ),
            )

        if salidas:
            items_salida = self._discount_existencias_pedido(
                plan=salidas,
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
            )
            resultados["salida"] = (
                self._registrar_movimiento_inventario_pedido(
                    pedido=pedido,
                    user=user,
                    items=items_salida,
                    tipo_movimiento=TipoMovimiento.SALIDA,
                    observaciones=(
                        f"Descuento automático por cambios aceptados en pedido {pedido.folio or pedido.pk}"
                    ),
                ),
                self._registrar_auditoria_inventario_pedido(
                    pedido=pedido,
                    user=user,
                    items=items_salida,
                    accion="SALIDA",
                    request=request,
                ),
            )

        return resultados

    def handle_get_onboarding(self, request):
        from catalogo.models import Producto, Color
        from terceros.models import Cliente, DireccionCliente
        from nucleo.models import SatRegimenFiscal
        
        user = request.user
        empresa = getattr(user, "empresa", None)

        limit_raw = request.query_params.get("limit")
        limit = None
        
        if limit_raw not in (None, "", "all", "ALL", "0", "-1"):
            try:
                limit = int(limit_raw)
            except Exception:
                limit = 20
            limit = max(1, min(limit, 1000))

        empresa_id = request.query_params.get("empresa_id")
        
        if getattr(user, "is_superuser", False) and not empresa and empresa_id:
            empresa = Empresa.objects.filter(pk=empresa_id).first()

        cliente_q = (request.query_params.get("cliente_q") or "").strip()
        cliente_id_raw = (
            request.query_params.get("cliente_id")
            or request.query_params.get("cliente")
            or ""
        ).strip()
        
        producto_q = (request.query_params.get("producto_q") or "").strip()

        clientes_qs = Cliente.objects.filter(activo=True)
        productos_qs = Producto.objects.filter(activo=True)
        if not getattr(user, "is_superuser", False) and empresa:
            clientes_qs = clientes_qs.filter(empresa=empresa)
            productos_qs = productos_qs.filter(empresa=empresa)
        if getattr(user, "is_superuser", False) and empresa:
            clientes_qs = clientes_qs.filter(empresa=empresa)
            productos_qs = productos_qs.filter(empresa=empresa)
        if not getattr(user, "is_superuser", False) and not getattr(
            user, "is_admin_empresa", False
        ):
            clientes_qs = clientes_qs.filter(vendedores__id=getattr(user, "id", None))

        if cliente_q:
            clientes_qs = (
                clientes_qs.filter(razon_social__icontains=cliente_q)
                | clientes_qs.filter(nombre__icontains=cliente_q)
                | clientes_qs.filter(rfc__icontains=cliente_q)
            )

        if producto_q:
            from decimal import Decimal

            qtxt = producto_q
            cond = Q(nombre__icontains=qtxt) | Q(descripcion__icontains=qtxt)
            if qtxt.isdigit():
                try:
                    cond = cond | Q(pk=int(qtxt))
                except Exception:
                    pass
                try:
                    cond = cond | Q(precio_base=Decimal(qtxt))
                except Exception:
                    pass
            else:
                try:
                    cond = cond | Q(precio_base=Decimal(qtxt))
                except Exception:
                    pass
            productos_qs = productos_qs.filter(cond)

        clientes_qs = clientes_qs.order_by("id").values(
            "id",
            "razon_social",
            "nombre",
            "rfc",
            "correo",
            "telefono",
            "direccion_fiscal",
            "colonia",
            "codigo_postal",
            "ciudad",
            "estado",
            "giro_empresarial",
            "sat_regimen_fiscal_id",
            "sat_regimen_fiscal__codigo",
            "sat_regimen_fiscal__descripcion",
            "sat_uso_cfdi_id",
            "sat_uso_cfdi__codigo",
            "sat_uso_cfdi__descripcion",
        )

        productos_qs = productos_qs.order_by("id").values(
            "id",
            "nombre",
            "descripcion",
            "precio_base",
        )

        if limit is not None:
            clientes_qs = clientes_qs[:limit]
            productos_qs = productos_qs[:limit]
        
        clientes = list(clientes_qs)
        productos = list(productos_qs)

        # Annotate each product with its variants (sku + color + talla) from variantes_producto
        from catalogo.models import ProductoVariante

        producto_ids = [p["id"] for p in productos]
        variantes_qs = (
            ProductoVariante.objects.filter(producto_id__in=producto_ids, activo=True)
            .values(
                "producto_id",
                "sku",
                "color_id",
                "color__nombre",
                "color__codigo_hex",
                "talla_id",
                "talla__nombre",
            )
            .order_by("producto_id", "color_id", "talla_id")
        )

        variantes_por_producto: dict = {}
        for v in variantes_qs:
            pid = v["producto_id"]
            if pid not in variantes_por_producto:
                variantes_por_producto[pid] = []
            variantes_por_producto[pid].append(
                {
                    "sku": v["sku"],
                    "color": {
                        "id": v["color_id"],
                        "nombre": v["color__nombre"],
                        "codigo_hex": v["color__codigo_hex"],
                    },
                    "talla": {
                        "id": v["talla_id"],
                        "nombre": v["talla__nombre"],
                    },
                }
            )
        for p in productos:
            p["variantes"] = variantes_por_producto.get(p["id"], [])

        colores = list(
            Color.objects.filter(activo=True)
            .order_by("id")
            .values("id", "nombre", "codigo", "codigo_hex")
        )
        direcciones_envio = []

        if cliente_id_raw:
            try:
                cliente_id = int(cliente_id_raw)
            except Exception:
                cliente_id = None
            if cliente_id:
                direcciones_qs = DireccionCliente.objects.filter(
                    activo=True, cliente_id=cliente_id
                )
                if not getattr(user, "is_superuser", False) and empresa:
                    direcciones_qs = direcciones_qs.filter(empresa=empresa)
                elif getattr(user, "is_superuser", False) and empresa:
                    direcciones_qs = direcciones_qs.filter(empresa=empresa)
                direcciones_envio = list(
                    direcciones_qs.order_by("id").values(
                        "id",
                        "destinatario",
                        "empresa_envio",
                        "telefono_envio",
                        "celular_envio",
                        "direccion_envio",
                        "colonia_envio",
                        "codigo_postal",
                        "ciudad_envio",
                        "estado_envio",
                        "referencias",
                        "is_default",
                    )
                )
        regimenes_fiscales = list(
            SatRegimenFiscal.objects.filter(activo=True)
            .order_by("codigo")
            .values("codigo", "descripcion")
        )
        regimenes_fiscales = [
            {"value": r["codigo"], "label": f"{r['codigo']} - {r['descripcion']}"}
            for r in regimenes_fiscales
        ]
        tipos_pedido = [
            {"value": tp[0], "label": tp[1]} for tp in Pedido.CHOICES_TIPO_PEDIDO
        ]
        return  {
                "vendedor": {
                    "id": getattr(user, "pk", None),
                    "username": getattr(user, "username", None),
                    "email": getattr(user, "email", None),
                    "empresa_id": getattr(empresa, "pk", None),
                },
                "catalogos": {
                    "formas_pago": [
                        {"value": k, "label": v}
                        for k, v in Cotizacion.FormaPago.choices
                    ],
                    "metodos_pago": [
                        {"value": k, "label": v}
                        for k, v in Cotizacion.MetodoPago.choices
                    ],
                    "usos_cfdi": [
                        {"value": k, "label": v} for k, v in Cotizacion.UsoCfdi.choices
                    ],
                    "colores": colores,
                    "tipos_pedido": tipos_pedido,
                    "regimenes_fiscales": regimenes_fiscales,
                },
                "busqueda": {
                    "clientes": clientes,
                    "productos": productos,
                    "direcciones_envio": direcciones_envio,
                },
            }

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    # TODO: SAVE OP
    def onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)

        if request.method.lower() == "get":
            res = self.handle_get_onboarding(request)
            return Response(res)

        raw = request.data
        if isinstance(raw, dict):
            raw_dict = raw
        else:
            try:
                raw_dict = dict(raw)
            except Exception:
                raw_dict = {}

        if "cotizacion" not in raw_dict:
            if "pedido" in raw_dict:
                cotizacion_payload = raw_dict.get("pedido") or {}
            else:
                cotizacion_payload = {
                    k: v
                    for k, v in raw_dict.items()
                    if k
                    not in {"detalle", "detalles", "cotizacion_id", "servicios_extras"}
                }
            # filtrar solo campos válidos del modelo
            try:
                from ventas.models import Cotizacion as CotModel

                allowed = {
                    f.name
                    for f in CotModel._meta.get_fields()
                    if getattr(f, "concrete", False)
                }
                cotizacion_payload = {
                    k: v for k, v in cotizacion_payload.items() if k in allowed
                }
                for k in [
                    "empresa",
                    "vendedor",
                    "estatus",
                    "created_at",
                    "updated_at",
                    "autorizada_at",
                    "cambios_solicitados_at",
                    "aprobado_snapshot",
                ]:
                    cotizacion_payload.pop(k, None)
            except Exception:
                pass
            normalized = {
                "cotizacion_id": raw_dict.get("cotizacion_id")
                or (
                    cotizacion_payload.get("id")
                    if isinstance(cotizacion_payload, dict)
                    else None
                ),
                "cotizacion": cotizacion_payload,
                "detalle": raw_dict.get("detalle") or raw_dict.get("detalles") or [],
                "servicios_extras": raw_dict.get("servicios_extras") or [],
            }
            if normalized.get("cotizacion_id") in (None, ""):
                normalized.pop("cotizacion_id", None)
        else:
            normalized = raw_dict
            if isinstance(normalized, dict) and normalized.get("cotizacion_id") in (
                None,
                "",
            ):
                normalized = dict(normalized)
                normalized.pop("cotizacion_id", None)

        serializer = CotizacionOnboardingCreateSerializer(data=normalized)
        serializer.is_valid(raise_exception=True)
        cotizacion_id = serializer.validated_data.get("cotizacion_id") or (
            request.data.get("cotizacion") or {}
        ).get("id")
        
        cotizacion_data = serializer.validated_data["cotizacion"]
        detalle_data = serializer.validated_data["detalle"]
        servicios_extras_data = serializer.validated_data.get("servicios_extras") or []

        if not getattr(user, "is_superuser", False) and not empresa:
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

        with transaction.atomic():
            edit_minutes = int(getattr(settings, "COTIZACION_EDIT_WINDOW_MINUTES", 30))
            edit_minutes = max(1, edit_minutes)
            now = timezone.now()

            try:
                if cotizacion_id:
                    cotizacion = (
                        Cotizacion.objects.select_for_update()
                        .filter(pk=cotizacion_id)
                        .first()
                    )
                    if not cotizacion:
                        raise ValidationError({"cotizacion_id": "Cotización no encontrada."})
                    if (
                        not getattr(user, "is_superuser", False)
                        and empresa
                        and cotizacion.empresa_id != empresa.pk
                    ):
                        raise ValidationError({"cotizacion_id": "No tienes acceso a esta cotización."})
                    if not getattr(user, "is_superuser", False) and not getattr(user, "is_admin_empresa", False):
                        if (cotizacion.vendedor_id and cotizacion.vendedor_id != getattr(user, "id", None)
                        ):
                            raise ValidationError({"cotizacion_id": "No tienes acceso a esta cotización."})

                    if cotizacion.estatus == 3 and cotizacion.autorizada_at:
                        limite = cotizacion.autorizada_at + timedelta(
                            minutes=edit_minutes
                        )
                        if (
                            now > limite
                            and not getattr(user, "is_superuser", False)
                            and not getattr(user, "is_admin_empresa", False)
                        ):
                            raise ValidationError(
                                {
                                    "cotizacion_id": "La cotización ya no está dentro del periodo permitido para edición."
                                }
                            )
                        cotizacion.estatus = 5
                        cotizacion.cambios_solicitados_at = now
                    elif cotizacion.estatus == 4:
                        cotizacion.estatus = 1

                    for k, v in cotizacion_data.items():
                        setattr(cotizacion, k, v)
                    update_fields = list(cotizacion_data.keys())
                    update_fields += ["estatus", "cambios_solicitados_at"]
                    cotizacion.save(update_fields=list(dict.fromkeys(update_fields)))
                else:
                    cotizacion = Cotizacion.objects.create(
                        empresa=empresa, vendedor=user, estatus=1, **cotizacion_data
                    )
            except TypeError:
                logger.exception("TypeError al crear/actualizar cotización.")
                raise ValidationError({"cotizacion": "Datos inválidos."})
            except ValueError:
                logger.exception("ValueError al crear/actualizar cotización.")
                raise ValidationError({"cotizacion": "Datos inválidos."})

            _save_cotizacion_detalle(cotizacion, detalle_data, empresa, user)
            _save_servicios_extras(cotizacion, servicios_extras_data)
            
            cotizacion = Cotizacion.objects.filter(pk=cotizacion.pk).first()
            detalles_qs = CotizacionDetalle.objects.filter(
                cotizacion=cotizacion
            ).prefetch_related("tallas")
            servicios_extras_qs = CotizacionServicioExtra.objects.filter(
                cotizacion=cotizacion
            ).order_by("id")
            return Response(
                {
                    "cotizacion": CotizacionSerializer(cotizacion).data,
                    "detalles": CotizacionDetalleWithTallasSerializer(detalles_qs, many=True).data,
                    "servicios_extras": list(
                        servicios_extras_qs.values(
                            "id", "nombre", "monto", "cantidad", "visible_en_factura"
                        )
                    ),
                    "pedido": None,
                },
                status=status.HTTP_201_CREATED,
            )

    def _require_mesa_control(self, user):
        if getattr(user, "is_superuser", False):
            return
        if getattr(user, "is_admin_empresa", False):
            return
        raise ValidationError(
            {"permiso": "Acción disponible solo para mesa de control."}
        )

    def _copiar_cotizacion_a_pedido(self, cotizacion, empresa):
        pedido = Pedido.objects.create(
            empresa=empresa,
            sucursal=cotizacion.sucursal,
            cliente=cotizacion.cliente,
            cotizacion=cotizacion,
            moneda=cotizacion.moneda,
            tipo_pedido=1,
            estatus=3,
            recompra=cotizacion.recompra,
            chat_online=cotizacion.chat_online,
            pedido_online=cotizacion.pedido_online,
            prospeccion=cotizacion.prospeccion,
            recomendacion=cotizacion.recomendacion,
            amazon=cotizacion.amazon,
            google=cotizacion.google,
            publicidad=cotizacion.publicidad,
            mercado_libre=cotizacion.mercado_libre,
            redes_sociales=cotizacion.redes_sociales,
            otro=cotizacion.otro,
            mailing=cotizacion.mailing,
            persona_pagos=cotizacion.persona_pagos,
            correo_facturas=cotizacion.correo_facturas,
            telefono_pagos=cotizacion.telefono_pagos,
            oc=cotizacion.oc,
            forma_pago=cotizacion.forma_pago,
            metodo_pago=cotizacion.metodo_pago,
            uso_cfdi=cotizacion.uso_cfdi,
            anticipo_total=cotizacion.anticipo_total,
            anticipo_parcial=cotizacion.anticipo_parcial,
            vendedor_autoriza=cotizacion.vendedor_autoriza,
            pago_antes_embarque=cotizacion.pago_antes_embarque,
            por_confirmar=cotizacion.por_confirmar,
            otra_cantidad=cotizacion.otra_cantidad,
            monto=cotizacion.monto,
            empaque_ecologico=cotizacion.empaque_ecologico,
            embarque_parcial=cotizacion.embarque_parcial,
            comentarios_parcialidad=cotizacion.comentarios_parcialidad,
            destinatario=cotizacion.destinatario,
            empresa_envio=cotizacion.empresa_envio,
            telefono_envio=cotizacion.telefono_envio,
            celular_envio=cotizacion.celular_envio,
            direccion_envio=cotizacion.direccion_envio,
            colonia_envio=cotizacion.colonia_envio,
            codigo_postal=cotizacion.codigo_postal,
            ciudad_envio=cotizacion.ciudad_envio,
            estado_envio=cotizacion.estado_envio,
            referencias=cotizacion.referencias,
            envio=cotizacion.envio,
            programa_bordados=cotizacion.programa_bordados,
            bordado_pantalones_extras=cotizacion.bordado_pantalones_extras,
            bordado_logotipo=cotizacion.bordado_logotipo,
            serigrafia=cotizacion.serigrafia,
            reflejante=cotizacion.reflejante,
            observaciones=cotizacion.observaciones,
            flete=cotizacion.flete,
            seguros=cotizacion.seguros,
            anticipo=cotizacion.anticipo,
            subtotal=cotizacion.subtotal,
            descuento_global=cotizacion.descuento_global,
            ieps=cotizacion.ieps,
            iva=cotizacion.iva,
            gran_total=cotizacion.gran_total,
        )
        self._asignar_folio_pedido(pedido, empresa)
        self._snapshot_facturacion_pedido(pedido)

        detalles = (
            CotizacionDetalle.objects.filter(cotizacion=cotizacion)
            .prefetch_related("tallas")
            .order_by("id")
        )
        
        for det in detalles:
            pedido_det = PedidoDetalle.objects.create(
                pedido=pedido,
                producto=det.producto,
                color_id=getattr(det, "color_id", None),
                direccion_envio_cliente_id=getattr(
                    det, "direccion_envio_cliente_id", None
                ),
                precio_lista=det.precio_lista,
                precio_unitario=det.precio_unitario,
                costo_unitario=det.costo_unitario,
                subtotal_linea=det.subtotal_linea,
            )
            for t in det.tallas.all():
                PedidoDetalleTalla.objects.create(
                    pedido_detalle=pedido_det,
                    talla=t.talla,
                    cantidad=t.cantidad,
                    precio_unitario=t.precio_unitario,
                    subtotal_talla=t.subtotal_talla,
                    lleva_bordado=t.lleva_bordado,
                    bordado_config=t.bordado_config,
                    lleva_reflejante=t.lleva_reflejante,
                    reflejante_config=t.reflejante_config,
                    lleva_corte_manga=t.lleva_corte_manga,
                    corte_manga_config=t.corte_manga_config,
                    lleva_cambio_talla=t.lleva_cambio_talla,
                    cambio_talla_config=t.cambio_talla_config,
                    variante=t.variante,
                )
                
        for s in CotizacionServicioExtra.objects.filter(cotizacion=cotizacion).order_by("id"):
            PedidoServicioExtra.objects.create(
                pedido=pedido,
                nombre=s.nombre,
                monto=s.monto,
                visible_en_factura=s.visible_en_factura,
            )

        # Generar Órdenes de Trabajo (Automáticas)
        # Envolvemos en try/except para que un error en la generación de órdenes
        # no detenga la autorización del pedido principal (fail-safe).
        try:
            # Generar Órdenes de Bordado (OB) automáticamente
            self._generar_ordenes_bordado(pedido, empresa)
            # Generar Órdenes de Reflejante (OR) automáticamente
            self._generar_ordenes_reflejante(pedido, empresa)
            # Generar Orden de Producción (OP) automáticamente
            self._generar_orden_produccion(pedido, empresa)
            # Generar Órdenes de Corte de Manga (OCM) automáticamente
            self._generar_ordenes_corte_manga(pedido, empresa)
        except Exception as e:
            # Aquí se podría loguear el error: logger.error(f"Error generando órdenes: {e}")
            pass

        return pedido

    def _generar_ordenes_corte_manga(self, pedido, empresa):
        """
        Genera órdenes de corte de manga para los productos del pedido que tengan 'lleva_corte_manga=True'.
        """
        detalles_con_tallas_corte = PedidoDetalleTalla.objects.filter(
            pedido_detalle__pedido=pedido, lleva_corte_manga=True, cantidad__gt=0
        ).select_related("pedido_detalle__producto", "pedido_detalle__color", "talla")

        if not detalles_con_tallas_corte.exists():
            return

        with transaction.atomic():
            serie_folio = (
                SerieFolio.objects.select_for_update()
                .filter(
                    empresa=empresa,
                    sucursal=pedido.sucursal,
                    tipo_documento__iexact="ORDEN_CORTE_MANGA",
                    activo=True,
                )
                .order_by("id_serie_folio")
                .first()
            )

            folio_ocm = None
            if serie_folio:
                try:
                    folio_ocm, nuevo_consecutivo, anio_actual = (
                        serie_folio.get_siguiente_folio()
                    )
                    serie_folio.folio_actual = nuevo_consecutivo
                    serie_folio.ultimo_anio = anio_actual
                    serie_folio.save(
                        update_fields=["folio_actual", "ultimo_anio", "updated_at"]
                    )
                except Exception:
                    pass

            if not folio_ocm:
                folio_ocm = f"OCM-{pedido.folio or pedido.id}"

            ocm = OrdenesCorteManga.objects.create(
                empresa=empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_ocm=folio_ocm,
                estatus_corte=1,  # PENDIENTE
                prioridad=1,
            )

            for dt in detalles_con_tallas_corte:
                OrdenCorteMangaDetalle.objects.create(
                    ocm=ocm,
                    pedido_detalle=dt.pedido_detalle,
                    producto=dt.pedido_detalle.producto,
                    cantidad=dt.cantidad,
                    talla=dt.talla,
                    color=dt.pedido_detalle.color,
                    configuracion=dt.corte_manga_config,
                )

    def _generar_orden_produccion(self, pedido, empresa):
        """
        Genera una Orden de Producción (OP) para el pedido.
        """
        with transaction.atomic():
            # Intentar obtener serie para ORDEN_PRODUCCION
            serie_folio = (
                SerieFolio.objects.select_for_update()
                .filter(
                    empresa=empresa,
                    sucursal=pedido.sucursal,
                    tipo_documento__iexact="ORDEN_PRODUCCION",
                    activo=True,
                )
                .order_by("id_serie_folio")
                .first()
            )

            folio_op = None
            if serie_folio:
                try:
                    folio_op, nuevo_consecutivo, anio_actual = (
                        serie_folio.get_siguiente_folio()
                    )
                    serie_folio.folio_actual = nuevo_consecutivo
                    serie_folio.ultimo_anio = anio_actual
                    serie_folio.save(
                        update_fields=["folio_actual", "ultimo_anio", "updated_at"]
                    )
                except Exception:
                    pass

            if not folio_op:
                # Fallback: Folio basado en el folio del pedido
                folio_op = f"OP-{pedido.folio or pedido.id}"

            # Crear la Orden de Producción
            OrdenProduccion.objects.create(
                empresa=empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_op=folio_op,
                estatus_op=1,  # PENDIENTE
                prioridad=1,
            )

    def _generar_ordenes_bordado(self, pedido, empresa):
        """
        Genera órdenes de bordado para los productos del pedido que tengan 'lleva_bordado=True'.
        """
        # Obtener todas las tallas del pedido que requieren bordado
        detalles_con_tallas_bordado = PedidoDetalleTalla.objects.filter(
            pedido_detalle__pedido=pedido, lleva_bordado=True, cantidad__gt=0
        ).select_related("pedido_detalle__producto", "pedido_detalle__color", "talla")

        if not detalles_con_tallas_bordado.exists():
            return

        with transaction.atomic():
            # Intentar obtener serie para ORDEN_BORDADO
            serie_folio = (
                SerieFolio.objects.select_for_update()
                .filter(
                    empresa=empresa,
                    sucursal=pedido.sucursal,
                    tipo_documento__iexact="ORDEN_BORDADO",
                    activo=True,
                )
                .order_by("id_serie_folio")
                .first()
            )

            folio_ob = None
            if serie_folio:
                try:
                    folio_ob, nuevo_consecutivo, anio_actual = (
                        serie_folio.get_siguiente_folio()
                    )
                    serie_folio.folio_actual = nuevo_consecutivo
                    serie_folio.ultimo_anio = anio_actual
                    serie_folio.save(
                        update_fields=["folio_actual", "ultimo_anio", "updated_at"]
                    )
                except Exception:
                    pass

            if not folio_ob:
                # Fallback: Folio basado en el folio del pedido
                folio_ob = f"OB-{pedido.folio or pedido.id}"

            # Crear la Orden de Bordado maestra
            ob = OrdenesBordado.objects.create(
                empresa=empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_bordado=folio_ob,
                estatus_bordado=1,  # PENDIENTE
                prioridad=1,
            )

            # Crear el detalle de la Orden de Bordado
            for dt in detalles_con_tallas_bordado:
                OrdenBordadoDetalle.objects.create(
                    ob=ob,
                    pedido_detalle=dt.pedido_detalle,
                    producto=dt.pedido_detalle.producto,
                    cantidad=dt.cantidad,
                    talla=dt.talla,
                    color=dt.pedido_detalle.color,
                    # Configuración inicial (se puede pulir después)
                    posicion_bordado=(
                        dt.bordado_config.get("posicion") if dt.bordado_config else None
                    ),
                    colores_hilo=0,
                    puntadas=0,
                )

    def _generar_ordenes_reflejante(self, pedido, empresa):
        """
        Genera órdenes de reflejante para los productos del pedido que tengan 'lleva_reflejante=True'.
        """
        # Obtener todas las tallas del pedido que requieren reflejante
        detalles_con_tallas_reflejante = PedidoDetalleTalla.objects.filter(
            pedido_detalle__pedido=pedido, lleva_reflejante=True, cantidad__gt=0
        ).select_related("pedido_detalle__producto", "pedido_detalle__color", "talla")

        if not detalles_con_tallas_reflejante.exists():
            return

        with transaction.atomic():
            # Intentar obtener serie para ORDEN_REFLEJANTE
            serie_folio = (
                SerieFolio.objects.select_for_update()
                .filter(
                    empresa=empresa,
                    sucursal=pedido.sucursal,
                    tipo_documento__iexact="ORDEN_REFLEJANTE",
                    activo=True,
                )
                .order_by("id_serie_folio")
                .first()
            )

            folio_or = None
            if serie_folio:
                try:
                    folio_or, nuevo_consecutivo, anio_actual = (
                        serie_folio.get_siguiente_folio()
                    )
                    serie_folio.folio_actual = nuevo_consecutivo
                    serie_folio.ultimo_anio = anio_actual
                    serie_folio.save(
                        update_fields=["folio_actual", "ultimo_anio", "updated_at"]
                    )
                except Exception:
                    pass

            if not folio_or:
                # Fallback: Folio basado en el folio del pedido
                folio_or = f"OR-{pedido.folio or pedido.id}"

            # Crear la Orden de Reflejante maestra
            orden_r = OrdenesReflejante.objects.create(
                empresa=empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                folio_reflejante=folio_or,
                estatus_reflejante=1,  # PENDIENTE
                prioridad=1,
            )

            # Crear el detalle de la Orden de Reflejante 
            for dt in detalles_con_tallas_reflejante:
                OrdenReflejanteDetalle.objects.create(
                    orden_r=orden_r,
                    pedido_detalle=dt.pedido_detalle,
                    producto=dt.pedido_detalle.producto,
                    cantidad=dt.cantidad,
                    talla=dt.talla,
                    color=dt.pedido_detalle.color,
                    # Configuración inicial
                    tipo_reflejante=(
                        dt.reflejante_config.get("tipo")
                        if dt.reflejante_config
                        else None
                    ),
                    posicion=(
                        dt.reflejante_config.get("posicion")
                        if dt.reflejante_config
                        else None
                    ),
                    metros=0,
                )

    def _aplicar_cotizacion_a_pedido(self, cotizacion, pedido):
        pedido.empresa = cotizacion.empresa
        pedido.sucursal = cotizacion.sucursal
        pedido.cliente = cotizacion.cliente
        pedido.moneda = cotizacion.moneda
        pedido.recompra = cotizacion.recompra
        pedido.chat_online = cotizacion.chat_online
        pedido.pedido_online = cotizacion.pedido_online
        pedido.prospeccion = cotizacion.prospeccion
        pedido.recomendacion = cotizacion.recomendacion
        pedido.amazon = cotizacion.amazon
        pedido.google = cotizacion.google
        pedido.publicidad = cotizacion.publicidad
        pedido.mercado_libre = cotizacion.mercado_libre
        pedido.redes_sociales = cotizacion.redes_sociales
        pedido.otro = cotizacion.otro
        pedido.mailing = cotizacion.mailing
        pedido.persona_pagos = cotizacion.persona_pagos
        pedido.correo_facturas = cotizacion.correo_facturas
        pedido.telefono_pagos = cotizacion.telefono_pagos
        pedido.oc = cotizacion.oc
        pedido.forma_pago = cotizacion.forma_pago
        pedido.metodo_pago = cotizacion.metodo_pago
        pedido.uso_cfdi = cotizacion.uso_cfdi
        pedido.anticipo_total = cotizacion.anticipo_total
        pedido.anticipo_parcial = cotizacion.anticipo_parcial
        pedido.vendedor_autoriza = cotizacion.vendedor_autoriza
        pedido.pago_antes_embarque = cotizacion.pago_antes_embarque
        pedido.por_confirmar = cotizacion.por_confirmar
        pedido.otra_cantidad = cotizacion.otra_cantidad
        pedido.monto = cotizacion.monto
        pedido.empaque_ecologico = cotizacion.empaque_ecologico
        pedido.embarque_parcial = cotizacion.embarque_parcial
        pedido.comentarios_parcialidad = cotizacion.comentarios_parcialidad
        pedido.destinatario = cotizacion.destinatario
        pedido.empresa_envio = cotizacion.empresa_envio
        pedido.telefono_envio = cotizacion.telefono_envio
        pedido.celular_envio = cotizacion.celular_envio
        pedido.direccion_envio = cotizacion.direccion_envio
        pedido.colonia_envio = cotizacion.colonia_envio
        pedido.codigo_postal = cotizacion.codigo_postal
        pedido.ciudad_envio = cotizacion.ciudad_envio
        pedido.estado_envio = cotizacion.estado_envio
        pedido.referencias = cotizacion.referencias
        pedido.envio = cotizacion.envio
        pedido.programa_bordados = cotizacion.programa_bordados
        pedido.bordado_pantalones_extras = cotizacion.bordado_pantalones_extras
        pedido.bordado_logotipo = cotizacion.bordado_logotipo
        pedido.serigrafia = cotizacion.serigrafia
        pedido.reflejante = cotizacion.reflejante
        pedido.observaciones = cotizacion.observaciones
        pedido.flete = cotizacion.flete
        pedido.seguros = cotizacion.seguros
        pedido.anticipo = cotizacion.anticipo
        pedido.subtotal = cotizacion.subtotal
        pedido.descuento_global = cotizacion.descuento_global
        pedido.ieps = cotizacion.ieps
        pedido.iva = cotizacion.iva
        pedido.gran_total = cotizacion.gran_total
        pedido.save()

        PedidoDetalle.objects.filter(pedido=pedido).delete()
        PedidoServicioExtra.objects.filter(pedido=pedido).delete()
        detalles = (
            CotizacionDetalle.objects.filter(cotizacion=cotizacion)
            .prefetch_related("tallas")
            .order_by("id")
        )
        for det in detalles:
            pedido_det = PedidoDetalle.objects.create(
                pedido=pedido,
                producto=det.producto,
                color_id=getattr(det, "color_id", None),
                direccion_envio_cliente_id=getattr(
                    det, "direccion_envio_cliente_id", None
                ),
                precio_lista=det.precio_lista,
                precio_unitario=det.precio_unitario,
                costo_unitario=det.costo_unitario,
                subtotal_linea=det.subtotal_linea,
            )
            for t in det.tallas.all():
                PedidoDetalleTalla.objects.create(
                    pedido_detalle=pedido_det,
                    talla=t.talla,
                    cantidad=t.cantidad,
                    precio_unitario=t.precio_unitario,
                    subtotal_talla=t.subtotal_talla,
                    lleva_bordado=t.lleva_bordado,
                    bordado_config=t.bordado_config,
                    lleva_reflejante=t.lleva_reflejante,
                    reflejante_config=t.reflejante_config,
                    lleva_corte_manga=t.lleva_corte_manga,
                    corte_manga_config=t.corte_manga_config,
                    lleva_cambio_talla=t.lleva_cambio_talla,
                    cambio_talla_config=t.cambio_talla_config,
                    variante=t.variante,
                )
        for s in CotizacionServicioExtra.objects.filter(cotizacion=cotizacion).order_by(
            "id"
        ):
            PedidoServicioExtra.objects.create(
                pedido=pedido,
                nombre=s.nombre,
                monto=s.monto,
                visible_en_factura=s.visible_en_factura,
            )

    @action(detail=True, methods=["post"], url_path="enviar-revision")
    def enviar_revision(self, request, pk=None):
        cotizacion = self.get_object()
        user = request.user

        # Solo el vendedor o un admin pueden enviarla a revisión
        if not getattr(user, "is_superuser", False) and not getattr(
            user, "is_admin_empresa", False
        ):
            if cotizacion.vendedor_id and cotizacion.vendedor_id != user.id:
                raise ValidationError(
                    {
                        "permiso": "No tienes permiso para enviar esta cotización a revisión."
                    }
                )

        if cotizacion.estatus not in [
            1,
            4,
            5,
        ]:  # Borrador, Rechazada o Cambios Solicitados
            raise ValidationError(
                {"estatus": "La cotización ya está en revisión o autorizada."}
            )

        cotizacion.estatus = 2
        cotizacion.save(update_fields=["estatus", "updated_at"])

        return Response(
            {
                "ok": True,
                "estatus": "EN REVISION",
                "cotizacion": CotizacionSerializer(cotizacion).data,
            }
        )

    @action(detail=True, methods=["post"], url_path="autorizar")
    def autorizar(self, request, pk=None):
        user = request.user
        self._require_mesa_control(user)
        cotizacion = self.get_object()
        empresa = cotizacion.empresa

        with transaction.atomic():
            cotizacion = (
                Cotizacion.objects.select_for_update().filter(pk=cotizacion.pk).first()
            )

            if not cotizacion:
                raise ValidationError(
                    {"cotizacion": "No se encontró la cotización para autorizar."}
                )

            if cotizacion.estatus != 2:
                raise ValidationError({"cotizacion": "La cotización debe estar EN REVISION para ser autorizada."})

            pedido_existente = (
                Pedido.objects.select_for_update()
                .filter(cotizacion=cotizacion, activo=True)
                .order_by("-id")
                .first()
            )
            
            if pedido_existente:
                raise ValidationError({"cotizacion": "La cotización ya tiene un pedido generado."})

            pedido = self._copiar_cotizacion_a_pedido(cotizacion, empresa)
            self._descontar_existencias_pedido(
                pedido=pedido,
                user=user,
                request=request,
            )
            cotizacion.estatus = 3
            cotizacion.autorizada_at = timezone.now()
            cotizacion.cambios_solicitados_at = None
            cotizacion.aprobado_snapshot = self._snapshot_cotizacion(cotizacion)
            cotizacion.save(
                update_fields=[
                    "estatus",
                    "autorizada_at",
                    "cambios_solicitados_at",
                    "aprobado_snapshot",
                    "updated_at",
                ]
            )

        pedido = (
            Pedido.objects.filter(pk=pedido.pk)
            .prefetch_related("detalles__tallas")
            .first()
        )
        return Response(
            {
                "cotizacion": CotizacionSerializer(cotizacion).data,
                "pedido": PedidoSerializer(pedido).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="rechazar")
    def rechazar(self, request, pk=None):
        user = request.user
        self._require_mesa_control(user)
        cotizacion = self.get_object()
        with transaction.atomic():
            cotizacion = (
                Cotizacion.objects.select_for_update().filter(pk=cotizacion.pk).first()
            )
            if cotizacion.estatus == 3:
                raise ValidationError(
                    {"cotizacion": "La cotización ya está autorizada."}
                )
            cotizacion.estatus = 4
            cotizacion.save(update_fields=["estatus", "updated_at"])
        return Response({"cotizacion": CotizacionSerializer(cotizacion).data})

    @action(detail=True, methods=["post"], url_path="aceptar-cambios")
    def aceptar_cambios(self, request, pk=None):
        user = request.user
        self._require_mesa_control(user)
        cotizacion = self.get_object()
        with transaction.atomic():
            cotizacion = (
                Cotizacion.objects.select_for_update().filter(pk=cotizacion.pk).first()
            )
            if cotizacion.estatus != 5:
                raise ValidationError(
                    {"cotizacion": "La cotización no tiene cambios pendientes."}
                )
            pedido = (
                Pedido.objects.select_for_update()
                .filter(cotizacion=cotizacion, activo=True)
                .order_by("-id")
                .first()
            )
            if not pedido:
                raise ValidationError(
                    {"cotizacion": "No existe pedido para aplicar cambios."}
                )
            self._ajustar_existencias_cambios_pedido(
                cotizacion=cotizacion,
                pedido=pedido,
                user=user,
                request=request,
            )
            self._aplicar_cotizacion_a_pedido(cotizacion, pedido)
            cotizacion.estatus = 3
            cotizacion.cambios_solicitados_at = None
            cotizacion.aprobado_snapshot = self._snapshot_cotizacion(cotizacion)
            cotizacion.save(
                update_fields=[
                    "estatus",
                    "cambios_solicitados_at",
                    "aprobado_snapshot",
                    "updated_at",
                ]
            )
        return Response(
            {
                "cotizacion": CotizacionSerializer(cotizacion).data,
                "pedido_id": pedido.id,
            }
        )

    @action(detail=True, methods=["post"], url_path="rechazar-cambios")
    def rechazar_cambios(self, request, pk=None):
        user = request.user
        self._require_mesa_control(user)
        cotizacion = self.get_object()
        with transaction.atomic():
            cotizacion = (
                Cotizacion.objects.select_for_update().filter(pk=cotizacion.pk).first()
            )
            if cotizacion.estatus != 5:
                raise ValidationError(
                    {"cotizacion": "La cotización no tiene cambios pendientes."}
                )
            snapshot = cotizacion.aprobado_snapshot or {}
            snap_cot = snapshot.get("cotizacion") or {}
            snap_det = snapshot.get("detalles") or []
            snap_servicios = snapshot.get("servicios_extras") or []
            if not snap_cot:
                raise ValidationError(
                    {"cotizacion": "No hay snapshot aprobado para revertir."}
                )

            skip = {
                "id",
                "empresa",
                "empresa_id",
                "created_at",
                "updated_at",
                "aprobado_snapshot",
            }
            model_fields = {f.name for f in Cotizacion._meta.fields}
            for k, v in snap_cot.items():
                if k in skip:
                    continue
                if k in model_fields:
                    setattr(cotizacion, k, v)
            cotizacion.estatus = 3
            cotizacion.cambios_solicitados_at = None
            cotizacion.save(
                update_fields=["estatus", "cambios_solicitados_at", "updated_at"]
                + [k for k in snap_cot.keys() if k in model_fields and k not in skip]
            )

            CotizacionDetalle.objects.filter(cotizacion=cotizacion).delete()
            CotizacionServicioExtra.objects.filter(cotizacion=cotizacion).delete()
            for det in snap_det:
                prod_id = det.get("producto")
                if isinstance(prod_id, dict):
                    prod_id = prod_id.get("id")
                color_id = det.get("color")
                if isinstance(color_id, dict):
                    color_id = color_id.get("id")
                direccion_id = det.get("direccion_envio_cliente")
                if isinstance(direccion_id, dict):
                    direccion_id = direccion_id.get("id")
                cot_det = CotizacionDetalle.objects.create(
                    cotizacion=cotizacion,
                    producto_id=prod_id,
                    color_id=color_id or None,
                    direccion_envio_cliente_id=direccion_id or None,
                    precio_lista=det.get("precio_lista"),
                    precio_unitario=det.get("precio_unitario") or 0,
                    costo_unitario=det.get("costo_unitario"),
                    subtotal_linea=det.get("subtotal_linea") or 0,
                )
                for t in det.get("tallas") or []:
                    lleva_reflejante = bool(
                        t.get("lleva_reflejante") or t.get("lleva_serigrafia")
                    )
                    reflejante_config = t.get("reflejante_config")
                    if reflejante_config is None:
                        reflejante_config = t.get("serigrafia_config")
                    CotizacionDetalleTalla.objects.create(
                        cotizacion_detalle=cot_det,
                        talla_id=t.get("talla"),
                        cantidad=t.get("cantidad") or 1,
                        precio_unitario=t.get("precio_unitario"),
                        subtotal_talla=t.get("subtotal_talla") or 0,
                        lleva_bordado=bool(t.get("lleva_bordado")),
                        bordado_config=t.get("bordado_config"),
                        lleva_reflejante=lleva_reflejante,
                        reflejante_config=reflejante_config,
                        lleva_corte_manga=bool(t.get("lleva_corte_manga")),
                        corte_manga_config=t.get("corte_manga_config"),
                        lleva_cambio_talla=bool(t.get("lleva_cambio_talla")),
                        cambio_talla_config=t.get("cambio_talla_config"),
                        sku=t.get("sku"),
                    )
            for s in snap_servicios:
                CotizacionServicioExtra.objects.create(
                    cotizacion=cotizacion,
                    nombre=s.get("nombre") or "",
                    monto=s.get("monto") or 0,
                    cantidad=s.get("cantidad") or 1,
                    visible_en_factura=bool(s.get("visible_en_factura", True)),
                )
        return Response({"cotizacion": CotizacionSerializer(cotizacion).data})


class CotizacionDetalleViewSet(viewsets.ModelViewSet):
    queryset = CotizacionDetalle.objects.all()
    serializer_class = CotizacionDetalleSerializer
    http_method_names = ["get", "post", "patch"]

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(cotizacion__empresa=empresa)
        return qs.none()


class PedidoViewSet(viewsets.ModelViewSet):
    queryset = Pedido.objects.filter(activo=True)
    serializer_class = PedidoSerializer

    def _snapshot_facturacion(self, pedido):
        try:
            cl = pedido.cliente
        except Exception:
            cl = None
        if not cl:
            return
        updates = {}
        if not pedido.cliente_razon_social:
            updates["cliente_razon_social"] = cl.razon_social
        if not pedido.cliente_nombre:
            updates["cliente_nombre"] = cl.nombre
        if not pedido.cliente_rfc:
            updates["cliente_rfc"] = cl.rfc
        if not pedido.cliente_regimen_fiscal_id and getattr(
            cl, "sat_regimen_fiscal_id", None
        ):
            updates["cliente_regimen_fiscal_id"] = cl.sat_regimen_fiscal_id
        if not pedido.cliente_direccion_fiscal:
            updates["cliente_direccion_fiscal"] = cl.direccion_fiscal
        if not pedido.cliente_colonia:
            updates["cliente_colonia"] = cl.colonia
        if not pedido.cliente_codigo_postal:
            updates["cliente_codigo_postal"] = cl.codigo_postal
        if not pedido.cliente_ciudad:
            updates["cliente_ciudad"] = cl.ciudad
        if not pedido.cliente_estado:
            updates["cliente_estado"] = cl.estado
        if not pedido.cliente_giro_empresarial:
            updates["cliente_giro_empresarial"] = cl.giro_empresarial
        if updates:
            for k, v in updates.items():
                setattr(pedido, k, v)
            pedido.save(update_fields=list(updates.keys()))

    def _asignar_folio(self, pedido, empresa):
        if pedido.folio:
            return

        if pedido.serie_folio_id:
            serie_folio = (
                SerieFolio.objects.select_for_update()
                .filter(pk=pedido.serie_folio_id)
                .first()
            )
        else:
            serie_folio = (
                SerieFolio.objects.select_for_update()
                .filter(
                    empresa=empresa,
                    sucursal=pedido.sucursal,
                    tipo_documento__iexact="PEDIDO",
                    activo=True,
                )
                .order_by("id_serie_folio")
                .first()
            )

        if not serie_folio:
            raise ValidationError(
                {
                    "serie_folio": "No hay una Serie/Folio activa configurada para tipo_documento='Pedido' en esta sucursal."
                }
            )

        try:
            folio_formateado, nuevo_consecutivo, anio_actual = (
                serie_folio.get_siguiente_folio()
            )
        except Exception:
            raise ValidationError({"folio": "No se pudo generar el folio del pedido."})

        serie_folio.folio_actual = nuevo_consecutivo
        serie_folio.ultimo_anio = anio_actual
        serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])

        pedido.serie_folio = serie_folio
        pedido.folio = folio_formateado
        pedido.folio_consecutivo = nuevo_consecutivo
        pedido.save(update_fields=["serie_folio", "folio", "folio_consecutivo"])

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if not getattr(user, "is_superuser", False) and getattr(user, "empresa", None):
            qs = qs.filter(empresa=user.empresa)
        q = self.request.query_params.get("q") or self.request.query_params.get("folio")
        if q:
            qs = qs.filter(folio__icontains=q)
        return qs

    def perform_create(self, serializer):
        empresa = self.request.user.empresa
        with transaction.atomic():
            pedido = serializer.save(empresa=empresa)
            self._asignar_folio(pedido, empresa)
            self._snapshot_facturacion(pedido)

    def perform_destroy(self, instance):
        instance.soft_delete()


class PedidoDetalleViewSet(viewsets.ModelViewSet):
    queryset = PedidoDetalle.objects.all()
    serializer_class = PedidoDetalleSerializer
    http_method_names = ["get", "post", "patch"]

    @action(detail=True, methods=["post"])
    def anular(self, request, pk=None):
        return Response({"msg": "PedidoDetalleViewSet.anular"})


class PedidoDetalleTallaViewSet(viewsets.ModelViewSet):
    queryset = PedidoDetalleTalla.objects.all()
    serializer_class = PedidoDetalleTallaSerializer
    http_method_names = ["get", "post", "patch"]


class MesaControlViewSet(CotizacionViewSet):
    """
    ViewSet exclusivo para Mesa de Control para gestionar cotizaciones en revisión,
    ver stock y realizar acciones de autorización.
    """

    http_method_names = ["get", "post"]

    def get_queryset(self):
        user = self.request.user
        # Solo mesa de control o superusuarios
        if not getattr(user, "is_superuser", False) and not getattr(
            user, "is_admin_empresa", False
        ):
            return Cotizacion.objects.none()

        empresa = getattr(user, "empresa", None)
        qs = Cotizacion.objects.filter(estatus=2)  # Solo EN REVISION
        if empresa:
            qs = qs.filter(empresa=empresa)

        return qs.select_related(
            "cliente", "sucursal", "moneda", "vendedor"
        ).prefetch_related(
            Prefetch(
                "cotizaciondetalle",
                queryset=CotizacionDetalle.objects.select_related(
                    "producto"
                ).prefetch_related(
                    Prefetch(
                        "tallas",
                        queryset=CotizacionDetalleTalla.objects.select_related("talla", "variante"),
                    )
                ),
            )
        )

    @action(detail=True, methods=["get"], url_path="stock-detalle")
    def stock_detalle(self, request, pk=None):
        """
        Consulta el stock actual de cada producto/talla de la cotización.
        """
        cotizacion = self.get_object()
        from inventarios.models import Existencia
        from catalogo.models import ProductoVariante

        resultados = []
        detalles = CotizacionDetalle.objects.filter(
            cotizacion=cotizacion
        ).select_related("producto", "color")

        for det in detalles:
            item = {
                "producto": det.producto.nombre,
                "color": det.color.nombre if det.color else "N/A",
                "tallas": [],
            }
            for ct in det.tallas.all():
                # Usar el vínculo directo a la variante para obtener stock
                variante = ct.variante

                stock_total = 0
                if variante:
                    stock_total = (
                        Existencia.objects.filter(producto_variante=variante).aggregate(
                            total=Sum("stock")
                        )["total"]
                        or 0
                    )

                item["tallas"].append(
                    {
                        "talla": ct.talla.nombre,
                        "cantidad_pedida": ct.cantidad,
                        "stock_actual": stock_total,
                        "diferencia": stock_total - ct.cantidad,
                    }
                )
            resultados.append(item)

        return Response(resultados)
