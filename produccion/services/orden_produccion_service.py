from nucleo.middleware import ip_auditoria
from decimal import Decimal, ROUND_HALF_UP

from django.db import models, transaction
from rest_framework.exceptions import ValidationError

from auditoria.models import AuditoriaEvento
from catalogo.models import ProductoVariante
from inventarios.services import costeo
from inventarios.models import Existencia, MovimientoInventario, MovimientoInventarioDetalle, TipoMovimiento
from produccion.models import (
    BomDetalle,
    ConsumoProduccion,
    ConsumoProduccionDetalle,
    OrdenProduccion,
    OrdenProduccionDetalle,
    OrdenProduccionPedido,
)
from produccion.utils.folios import generate_op_folio


QTY_PRECISION = Decimal("0.0001")

class OrdenProduccionService:

    @staticmethod
    def _to_decimal(value):
        return Decimal(str(value or 0)).quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)

    @staticmethod
    def _cantidad_requerida(cantidad_op, bom_det):
        """Insumo que consume un renglón de la OP para un renglón del BOM:
        ``piezas × cantidad del BOM × (1 + desperdicio/100)`` a 4 decimales
        (``ROUND_HALF_UP``). Única fórmula del consumo real
        (``_build_consumption_plan``) y de ``habilitacion_con_desperdicio`` del
        detalle de la OP (EC-880), para que ambos totales cuadren."""
        cantidad_op = OrdenProduccionService._to_decimal(cantidad_op)
        cantidad_base = OrdenProduccionService._to_decimal(bom_det.cantidad)
        desperdicio = OrdenProduccionService._to_decimal(bom_det.desperdicio or 0)
        factor_desperdicio = Decimal("1") + (desperdicio / Decimal("100"))
        return (cantidad_op * cantidad_base * factor_desperdicio).quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)

    @staticmethod
    def _build_consumption_plan(op_rows, empresa):
        # Consolidamos el consumo por insumo para validar y descontar inventario
        # una sola vez por producto componente.
        plan = {}

        for idx, row in enumerate(op_rows, start=1):
            producto_variante = row.get("producto_variante")
            variante_produccion = row.get("variante_produccion")
            bom = row.get("bom")
            cantidad_op = OrdenProduccionService._to_decimal(row.get("cantidad"))

            if not producto_variante and not variante_produccion:
                raise ValidationError(
                    {"orden_produccion_detalle": f"Detalle #{idx}: producto_variante o variante_produccion es requerido."}
                )
            if not bom:
                raise ValidationError(
                    {"orden_produccion_detalle": f"Detalle #{idx}: no se resolvió un BOM válido."}
                )
            if bom.empresa_id != empresa.pk:
                raise ValidationError(
                    {"orden_produccion_detalle": f"Detalle #{idx}: el BOM no pertenece a la empresa actual."}
                )
            if variante_produccion is not None:
                if bom.variante_produccion_id != variante_produccion.pk:
                    raise ValidationError(
                        {"orden_produccion_detalle": f"Detalle #{idx}: el BOM no corresponde a la variante_produccion enviada."}
                    )
            elif bom.producto_variante_id and bom.producto_variante_id != producto_variante.pk:
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

                cantidad_requerida = OrdenProduccionService._cantidad_requerida(cantidad_op, bom_det)

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
            variante_ids = list(
                ProductoVariante.objects.filter(producto_id=producto.pk).values_list("pk", flat=True)
            )
            producto_filter = models.Q(producto_id=producto.pk)
            if variante_ids:
                producto_filter |= models.Q(producto_variante_id__in=variante_ids)

            existencias = list(
                Existencia.objects.select_for_update()
                .filter(
                    almacen__empresa_id=empresa.pk,
                    almacen__sucursal_id=sucursal.pk,
                )
                .filter(producto_filter)
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

        detalles = MovimientoInventarioDetalle.objects.bulk_create(
            [
                MovimientoInventarioDetalle(
                    movimiento_inventario=movimiento,
                    producto_id=item["producto_id"],
                    # Un consumo = una existencia: su variante (o ninguna).
                    producto_variante_id=item["producto_variante_id"],
                    ubicacion_origen_id=item["ubicacion_id"],
                    ubicacion_destino_id=None,
                    # El almacén de la existencia consumida, como en ventas (EC-821).
                    almacen_id=item["almacen_id"],
                    lote_id=None,
                    serie_id=None,
                    cantidad=item["cantidad_consumida"],
                    costo_unitario=Decimal("0"),
                )
                for item in consumos
            ]
        )
        # Los insumos salen al costo promedio vigente (#809).
        costeo.costear(op.empresa_id, [(d, -d.cantidad, None) for d in detalles], costeo.Origen.CONSUMO_OP)
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
            ip = ip_auditoria(request)
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
        pedidos = validated_data.pop("pedidos_ids", None)
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
        from produccion.api.serializers import ligar_pedidos_op
        ligar_pedidos_op(op, pedidos)

        consumos = OrdenProduccionService._discount_existencias(plan, empresa, sucursal)
        consumo = OrdenProduccionService._registrar_consumo(op, consumos)
        movimiento = OrdenProduccionService._registrar_movimiento_inventario(op, user, consumos)
        auditoria = OrdenProduccionService._registrar_auditoria(op, user, consumos, request=request)

        # Sin apartado: lo que consumió esta OP puede dejar sin material a una
        # pre-orden "Lista para producir" de la misma sucursal.
        from produccion.services.pre_orden_service import recalcular_listas
        transaction.on_commit(lambda: recalcular_listas(empresa.pk, sucursal.pk, op))

        return {
            "op": op,
            "consumo_produccion": consumo,
            "movimiento_inventario": movimiento,
            "auditoria_evento": auditoria,
        }
    
    @staticmethod
    def get_formatted_op_detalle(op_id):
        # Consultas fijas, sin importar renglones, consumos ni pedidos (EC-880):
        # OP con sus FK de encabezado (las etiquetas y ``pedido_vinculado`` del
        # serializer) + renglones activos con sus FK + insumos del BOM +
        # consumos + sus renglones + pedidos ligados con su cliente.
        op = OrdenProduccion.objects.filter(pk=op_id).select_related(
            'empresa', 'sucursal', 'pedido__cliente', 'usuario_asignado',
        ).prefetch_related(
            # Sólo renglones activos (EC-880), como el listado y los KPIs.
            models.Prefetch(
                'orden_produccion_detalle',
                queryset=OrdenProduccionDetalle.objects.filter(activo=True).select_related(
                    'producto_variante__producto',
                    'producto_variante__talla',
                    'producto_variante__color',
                    'variante_produccion__talla',
                    'variante_produccion__color',
                    'bom',
                ).prefetch_related(
                    models.Prefetch(
                        'bom__materia_prima_detalle',
                        queryset=BomDetalle.objects.select_related('componente', 'unidad'),
                    ),
                ),
            ),
            # ``unidad_medida``: la ``unidad`` de ``consumos_resumen`` (EC-880).
            models.Prefetch(
                'consumoproduccion_set__detalles',
                queryset=ConsumoProduccionDetalle.objects.select_related('producto__unidad_medida'),
            ),
            # ``pedidos[].cliente`` (EC-880): una consulta para todos los pedidos.
            models.Prefetch(
                'op_pedidos',
                queryset=OrdenProduccionPedido.objects.select_related('pedido__cliente'),
            ),
        ).first()

        if not op:
            return None

        products_data = {}

        for detail in op.orden_produccion_detalle.all():
            variant = detail.producto_variante
            if variant is not None:
                product = variant.producto
                if not product:
                    continue
                product_id, product_nombre = product.id, product.nombre
            elif detail.variante_produccion is not None:
                # Muestra sin catálogo: se agrupa por la línea de muestra que la
                # originó (cada talla es una variante de producción distinta).
                variant = detail.variante_produccion
                product_id = f"mp-{variant.pedido_detalle_id or variant.pk}"
                product_nombre = variant.nombre or variant.sku
            else:
                continue

            if product_id not in products_data:
                products_data[product_id] = {
                    "nombre": product_nombre,
                    "cantidades": {
                        "total": 0.0,
                        "tallas": {}
                    },
                    "habilitacion": {},
                    "habilitacion_con_desperdicio": {},
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
                    con_desperdicio = p_data["habilitacion_con_desperdicio"]
                    con_desperdicio[key] = con_desperdicio.get(key, Decimal("0")) + (
                        OrdenProduccionService._cantidad_requerida(detail.cantidad, bom_det)
                    )

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
                "habilitacion": habilitacion_list,
                # EC-880: misma forma que ``habilitacion`` pero con el desperdicio
                # del BOM y la fórmula del consumo real (``_cantidad_requerida``);
                # ``total`` como texto a 4 decimales, igual que ``consumos``.
                # ``habilitacion`` conserva su valor (sin desperdicio).
                "habilitacion_con_desperdicio": [
                    {"codigo": code, "descripcion": name, "unidad": unit, "total": str(total)}
                    for (code, name, unit), total in p_info["habilitacion_con_desperdicio"].items()
                ],
            })

        from produccion.api.serializers import OrdenProduccionSerializer
        res_data = OrdenProduccionSerializer(op, context={"pedidos_con_cliente": True}).data
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
        # EC-880: planeado/ajustes/actual por material, con la misma regla que
        # ``recalcular-consumos`` (``produccion.services.consumo_resumen``).
        from produccion.services.consumo_resumen import resumen_consumos_api
        res_data["consumos_resumen"] = resumen_consumos_api(
            detalle for consumo in op.consumoproduccion_set.all() for detalle in consumo.detalles.all()
        )
        return res_data
