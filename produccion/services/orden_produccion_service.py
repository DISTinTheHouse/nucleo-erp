from decimal import Decimal, ROUND_HALF_UP

from django.db import models, transaction
from rest_framework.exceptions import ValidationError

from auditoria.models import AuditoriaEvento
from inventarios.models import Existencia, MovimientoInventario, MovimientoInventarioDetalle, TipoMovimiento
from produccion.models import (
    ConsumoProduccion,
    ConsumoProduccionDetalle,
    OrdenProduccion,
    OrdenProduccionDetalle,
)
from produccion.utils.folios import generate_op_folio


QTY_PRECISION = Decimal("0.0001")

class OrdenProduccionService:

    @staticmethod
    def _to_decimal(value):
        return Decimal(str(value or 0)).quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)

    @staticmethod
    def _build_consumption_plan(op_rows, empresa):
        # Consolidamos el consumo por insumo para validar y descontar inventario
        # una sola vez por producto componente.
        plan = {}

        for idx, row in enumerate(op_rows, start=1):
            producto_variante = row.get("producto_variante")
            bom = row.get("bom")
            cantidad_op = OrdenProduccionService._to_decimal(row.get("cantidad"))

            if not producto_variante:
                raise ValidationError(
                    {"orden_produccion_detalle": f"Detalle #{idx}: producto_variante es requerido."}
                )
            if not bom:
                raise ValidationError(
                    {"orden_produccion_detalle": f"Detalle #{idx}: no se resolvió un BOM válido."}
                )
            if bom.empresa_id != empresa.pk:
                raise ValidationError(
                    {"orden_produccion_detalle": f"Detalle #{idx}: el BOM no pertenece a la empresa actual."}
                )
            if bom.producto_variante_id and bom.producto_variante_id != producto_variante.pk:
                raise ValidationError(
                    {"orden_produccion_detalle": f"Detalle #{idx}: el BOM no corresponde al producto_variante enviado."}
                )

            bom_detalles = list(
                bom.materia_prima_detalle.filter(activo=True).select_related("componente")
            )
            if not bom_detalles:
                raise ValidationError(
                    {"orden_produccion_detalle": f"Detalle #{idx}: la lista de materiales no contiene insumos activos."}
                )

            for bom_det in bom_detalles:
                componente = bom_det.componente
                if not componente:
                    raise ValidationError(
                        {"orden_produccion_detalle": f"Detalle #{idx}: existe un insumo sin producto en el BOM."}
                    )

                cantidad_base = OrdenProduccionService._to_decimal(bom_det.cantidad)
                if cantidad_base <= 0:
                    raise ValidationError(
                        {"orden_produccion_detalle": f"Detalle #{idx}: el insumo {componente.nombre} tiene cantidad inválida en el BOM."}
                    )

                desperdicio = OrdenProduccionService._to_decimal(bom_det.desperdicio or 0)
                factor_desperdicio = Decimal("1") + (desperdicio / Decimal("100"))
                cantidad_requerida = (
                    cantidad_op * cantidad_base * factor_desperdicio
                ).quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)

                if componente.pk not in plan:
                    plan[componente.pk] = {
                        "producto": componente,
                        "cantidad": Decimal("0.0000"),
                    }
                plan[componente.pk]["cantidad"] += cantidad_requerida

        return list(plan.values())

    @staticmethod
    def _discount_existencias(plan, empresa, sucursal):
        consumos = []

        for item in plan:
            producto = item["producto"]
            cantidad_requerida = item["cantidad"].quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)

            existencias = list(
                Existencia.objects.select_for_update()
                .select_related("almacen", "ubicacion", "producto_variante")
                .filter(
                    almacen__empresa_id=empresa.pk,
                    almacen__sucursal_id=sucursal.pk,
                )
                .filter(
                    models.Q(producto_id=producto.pk)
                    | models.Q(producto_variante__producto_id=producto.pk)
                )
                .order_by("-cantidad", "id")
            )

            disponible = sum(
                (OrdenProduccionService._to_decimal(ex.cantidad) for ex in existencias),
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

                cantidad_actual = OrdenProduccionService._to_decimal(existencia.cantidad)
                if cantidad_actual <= 0:
                    continue

                cantidad_consumida = min(cantidad_actual, restante).quantize(
                    QTY_PRECISION, rounding=ROUND_HALF_UP
                )
                cantidad_nueva = (cantidad_actual - cantidad_consumida).quantize(
                    QTY_PRECISION, rounding=ROUND_HALF_UP
                )

                existencia.cantidad = cantidad_nueva
                existencia.stock = max(int(cantidad_nueva), 0)
                existencia.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])

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
                        "cantidad_consumida": cantidad_consumida,
                    }
                )
                restante -= cantidad_consumida

        return consumos

    @staticmethod
    def _registrar_consumo(op, consumos):
        consumo = ConsumoProduccion.objects.create(op=op)
        resumen = {}

        for item in consumos:
            producto_id = item["producto_id"]
            if producto_id not in resumen:
                resumen[producto_id] = {
                    "producto": item["producto"],
                    "cantidad": Decimal("0.0000"),
                }
            resumen[producto_id]["cantidad"] += item["cantidad_consumida"]

        ConsumoProduccionDetalle.objects.bulk_create(
            [
                ConsumoProduccionDetalle(
                    consumo_produccion=consumo,
                    producto=data["producto"],
                    cantidad=data["cantidad"].quantize(QTY_PRECISION, rounding=ROUND_HALF_UP),
                )
                for data in resumen.values()
            ]
        )
        return consumo

    @staticmethod
    def _registrar_movimiento_inventario(op, user, consumos):
        movimiento = MovimientoInventario.objects.create(
            empresa=op.empresa,
            sucursal=op.sucursal,
            pedido=op.pedido,
            entrega_id=None,
            devolucion_id=None,
            ajuste_inventario_id=None,
            tipo_movimiento=TipoMovimiento.SALIDA,
            usuario=user,
            observaciones=f"Consumo automático de insumos para OP {op.folio_op}",
            recepcion_id=None,
            transferencia_id=None,
            op=op,
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
                    cantidad=item["cantidad_consumida"],
                    costo_unitario=Decimal("0"),
                )
                for item in consumos
            ]
        )
        return movimiento

    @staticmethod
    def _registrar_auditoria(op, user, consumos, request=None):
        if not op.empresa_id:
            return None

        items = [
            {
                "producto_id": item["producto_id"],
                "producto_variante_id": item["producto_variante_id"],
                "almacen_id": item["almacen_id"],
                "ubicacion_id": item["ubicacion_id"],
                "cantidad_before": str(item["cantidad_before"]),
                "cantidad_after": str(item["cantidad_after"]),
                "delta": str(-item["cantidad_consumida"]),
            }
            for item in consumos
        ]

        ip = None
        user_agent = None
        if request is not None:
            ip = request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR")
            user_agent = request.META.get("HTTP_USER_AGENT")

        return AuditoriaEvento.objects.create(
            empresa=op.empresa,
            usuario=user if getattr(user, "pk", None) else None,
            modulo="inventarios",
            accion="SALIDA",
            tabla="existencias",
            id_registro=str(op.op_id),
            antes_json={"items": items, "op_id": op.op_id, "folio_op": op.folio_op},
            despues_json={
                "empresa_id": op.empresa_id,
                "sucursal_id": op.sucursal_id,
                "op_id": op.op_id,
                "folio_op": op.folio_op,
                "items": items,
            },
            ip=ip,
            user_agent=user_agent,
        )

    @staticmethod
    @transaction.atomic
    def save_orden_produccion(validated_data, user, request=None):
        # La OP se crea solo si el BOM es consistente y existe inventario
        # suficiente para descontar automáticamente los insumos requeridos.
        op_rows = validated_data.pop("orden_produccion_detalle")
        empresa = validated_data["empresa"]
        sucursal = validated_data["sucursal"]
        plan = OrdenProduccionService._build_consumption_plan(op_rows, empresa)

        folio_op = generate_op_folio(empresa, sucursal)
        op = OrdenProduccion.objects.create(
            folio_op=folio_op,
            usuario_asignado=user,
            **validated_data,
        )
        bulk_data = []
        for row in op_rows:
            bulk_data.append(OrdenProduccionDetalle(op=op, **row))
        OrdenProduccionDetalle.objects.bulk_create(bulk_data)

        consumos = OrdenProduccionService._discount_existencias(plan, empresa, sucursal)
        consumo = OrdenProduccionService._registrar_consumo(op, consumos)
        movimiento = OrdenProduccionService._registrar_movimiento_inventario(op, user, consumos)
        auditoria = OrdenProduccionService._registrar_auditoria(op, user, consumos, request=request)

        return {
            "op": op,
            "consumo_produccion": consumo,
            "movimiento_inventario": movimiento,
            "auditoria_evento": auditoria,
        }
    
    @staticmethod
    def get_formatted_op_detalle(op_id):
        op = OrdenProduccion.objects.filter(pk=op_id).prefetch_related(
            'orden_produccion_detalle__producto_variante__producto',
            'orden_produccion_detalle__producto_variante__talla',
            'orden_produccion_detalle__producto_variante__color',
            'orden_produccion_detalle__bom__materia_prima_detalle__componente',
            'orden_produccion_detalle__bom__materia_prima_detalle__unidad',
            'consumoproduccion_set__detalles__producto',
        ).first()

        if not op:
            return None

        products_data = {}

        for detail in op.orden_produccion_detalle.all():
            variant = detail.producto_variante
            if not variant:
                continue
            product = variant.producto
            if not product:
                continue

            product_id = product.id
            if product_id not in products_data:
                products_data[product_id] = {
                    "nombre": product.nombre,
                    "cantidades": {
                        "total": 0.0,
                        "tallas": {}
                    },
                    "habilitacion": {}
                }

            p_data = products_data[product_id]

            # 1. Cantidades
            qty = float(detail.cantidad)
            p_data["cantidades"]["total"] += qty
            talla_name = variant.talla.nombre if variant.talla else "N/A"
            color_name = variant.color.nombre if variant.color else "N/A"
            talla_color_key = (talla_name, color_name)
            p_data["cantidades"]["tallas"][talla_color_key] = p_data["cantidades"]["tallas"].get(talla_color_key, 0.0) + qty

            # 2. Habilitacion (materia prima)
            if detail.bom:
                for bom_det in detail.bom.materia_prima_detalle.all():
                    if not bom_det.activo or not bom_det.componente:
                        continue

                    comp = bom_det.componente
                    code = comp.cod_proscai if comp.cod_proscai else (comp.codigo if comp.codigo else "")
                    name = comp.nombre
                    unit = bom_det.unidad.clave.upper() if bom_det.unidad else ""

                    bom_qty = float(bom_det.cantidad)
                    needed = qty * bom_qty

                    key = (code, name, unit)
                    p_data["habilitacion"][key] = p_data["habilitacion"].get(key, 0.0) + needed

        productos_list = []
        for p_id, p_info in products_data.items():
            total_qty = p_info["cantidades"]["total"]
            total_qty_formatted = int(total_qty) if total_qty.is_integer() else round(total_qty, 2)

            tallas_list = []
            for (t_name, c_name), t_qty in p_info["cantidades"]["tallas"].items():
                t_qty_formatted = int(t_qty) if t_qty.is_integer() else round(t_qty, 2)
                tallas_list.append({
                    "talla": t_name,
                    "color": c_name,
                    "cantidad": t_qty_formatted
                })

            habilitacion_list = []
            for (code, name, unit), raw_qty in p_info["habilitacion"].items():
                raw_qty_formatted = int(raw_qty) if raw_qty.is_integer() else round(raw_qty, 4)

                habilitacion_list.append({
                    "codigo": code,
                    "descripcion": name,
                    "unidad": unit,
                    "total": raw_qty_formatted
                })

            productos_list.append({
                "nombre": p_info["nombre"],
                "cantidades": {
                    "total": total_qty_formatted,
                    "tallas": tallas_list
                },
                "habilitacion": habilitacion_list
            })

        from produccion.api.serializers import OrdenProduccionSerializer
        res_data = OrdenProduccionSerializer(op).data
        res_data.pop("orden_produccion_detalle", None)
        res_data["op_info"] = op.folio_op
        res_data["productos"] = productos_list
        res_data["consumos"] = [
            {
                "consumo_produccion_id": consumo.consumo_produccion_id,
                "detalles": [
                    {
                        "producto": detalle.producto_id,
                        "producto_nombre": getattr(detalle.producto, "nombre", None),
                        "cantidad": str(detalle.cantidad),
                    }
                    for detalle in consumo.detalles.all()
                ],
            }
            for consumo in op.consumoproduccion_set.all()
        ]
        return res_data
