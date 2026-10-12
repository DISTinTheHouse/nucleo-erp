from decimal import Decimal, ROUND_HALF_UP

from django.db import models, transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from auditoria.models import AuditoriaEvento
from catalogo.models import Producto
from inventarios.models import Existencia, MovimientoInventario, MovimientoInventarioDetalle, TipoMovimiento
from produccion.models import ConsumoProduccion, ConsumoProduccionDetalle, OrdenProduccion
from produccion.services.orden_produccion_service import OrdenProduccionService

QTY_PRECISION = Decimal("0.0001")
ESTATUS_CERRADOS = (
    OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
    OrdenProduccion.EstatusOrdenProduccion.CANCELADO,
)


class RecalculoConsumoTelaService:
    """Ajusta el consumo de tela de una OP a lo realmente usado en corte.

    No edita el consumo original: registra la diferencia como un
    ``ConsumoProduccion`` nuevo (cantidad con signo) y un ``MovimientoInventario``
    de ajuste ligado a la OP. Sin pólizas: ningún servicio de ``finanzas``
    nace de movimientos de inventario y estos van a costo 0.
    """

    @staticmethod
    def _q(value):
        return Decimal(str(value)).quantize(QTY_PRECISION, rounding=ROUND_HALF_UP)

    @staticmethod
    def _devolver(op, producto, cantidad):
        """Regresa ``cantidad`` a la existencia de la que salió el consumo."""
        origen = (
            MovimientoInventarioDetalle.objects.filter(
                movimiento_inventario__op=op,
                movimiento_inventario__tipo_movimiento=TipoMovimiento.SALIDA,
                producto=producto,
            )
            .order_by("id")
            .first()
        )
        filtro = models.Q(producto_id=producto.pk) | models.Q(producto_variante__producto_id=producto.pk)
        qs = Existencia.objects.select_for_update().filter(
            almacen__empresa_id=op.empresa_id, almacen__sucursal_id=op.sucursal_id
        ).filter(filtro)
        existencia = None
        if origen is not None:
            existencia = qs.filter(
                producto_variante_id=origen.producto_variante_id, ubicacion_id=origen.ubicacion_origen_id
            ).first()
        existencia = existencia or qs.order_by("id").first()
        if existencia is None:
            raise ValidationError({"inventario": f"No hay existencia de {producto.nombre} donde regresar el sobrante."})
        antes = RecalculoConsumoTelaService._q(existencia.cantidad)
        existencia.cantidad = antes + cantidad
        existencia.stock = max(int(existencia.cantidad), 0)
        existencia.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])
        return {
            "producto": producto,
            "producto_id": producto.pk,
            "producto_variante_id": existencia.producto_variante_id,
            "almacen_id": existencia.almacen_id,
            "ubicacion_id": existencia.ubicacion_id,
            "cantidad_before": antes,
            "cantidad_after": existencia.cantidad,
            "cantidad_consumida": -cantidad,
        }

    @staticmethod
    @transaction.atomic
    def recalcular(op_id, empresa, cantidad_real, productos, user, request=None):
        """Escala el consumo de la OP a las piezas realmente cortadas.

        ``cantidad_real``: piezas reales (>= las planeadas de la OP).
        ``productos``: ids de insumos a recalcular, o ``None`` para todos los
        del consumo (el catálogo no distingue telas; el usuario elige)."""
        op = OrdenProduccion.objects.select_for_update().filter(pk=op_id, empresa=empresa).first()
        if op is None:
            return None
        if op.estatus_op in ESTATUS_CERRADOS:
            raise ValidationError({"op": f"La OP está {op.get_estatus_op_display().lower()}: no se puede recalcular."})

        planeadas = RecalculoConsumoTelaService._q(
            op.orden_produccion_detalle.filter(activo=True).aggregate(t=Sum("cantidad"))["t"] or 0
        )
        real = RecalculoConsumoTelaService._q(cantidad_real)
        if planeadas <= 0:
            raise ValidationError({"op": "La OP no tiene piezas planeadas."})
        if real < planeadas:
            raise ValidationError({
                "cantidad_real_corte": f"Lo real ({real}) no puede ser menor a lo planeado ({planeadas}): "
                "si se cortaron menos piezas, ajusta la OP."
            })
        factor = real / planeadas

        base = ConsumoProduccionDetalle.objects.filter(consumo_produccion__op=op)
        ids = set(base.values_list("producto_id", flat=True))
        if productos is not None:
            ajenos = set(productos) - ids
            if ajenos:
                raise ValidationError({"productos": f"No forman parte del consumo de la OP: {sorted(ajenos)}."})
            ids &= set(productos)
        if not ids:
            raise ValidationError({"productos": "La OP no tiene consumos que recalcular."})

        movimientos_salida, movimientos_entrada = [], []
        resumen = []
        for producto in Producto.objects.filter(pk__in=ids).order_by("pk"):
            filas = base.filter(producto=producto)
            primer = filas.order_by("consumo_produccion_id", "consumo_detalle_id").first()
            planeado = RecalculoConsumoTelaService._q(primer.cantidad)
            actual = RecalculoConsumoTelaService._q(filas.aggregate(t=Sum("cantidad"))["t"] or 0)
            objetivo = RecalculoConsumoTelaService._q(planeado * factor)
            delta = objetivo - actual
            if delta == 0:
                continue
            if delta > 0:
                consumos = OrdenProduccionService._discount_existencias(
                    [{"producto": producto, "cantidad": delta}], op.empresa, op.sucursal
                )
                movimientos_salida.extend(consumos)
            else:
                movimientos_entrada.append(RecalculoConsumoTelaService._devolver(op, producto, -delta))
            resumen.append((producto, delta, actual, objetivo))

        if not resumen:
            return {"op": op, "consumo_produccion": None, "movimientos": [], "cambios": []}

        consumo = ConsumoProduccion.objects.create(op=op)
        ConsumoProduccionDetalle.objects.bulk_create(
            [ConsumoProduccionDetalle(consumo_produccion=consumo, producto=p, cantidad=d) for p, d, _a, _r in resumen]
        )

        movimientos = []
        for tipo, items in ((TipoMovimiento.SALIDA, movimientos_salida), (TipoMovimiento.ENTRADA, movimientos_entrada)):
            if not items:
                continue
            mov = MovimientoInventario.objects.create(
                empresa=op.empresa, sucursal=op.sucursal, pedido=op.pedido, tipo_movimiento=tipo,
                usuario=user, op=op, observaciones=f"Recálculo de consumo por piezas reales OP {op.folio_op}",
            )
            MovimientoInventarioDetalle.objects.bulk_create([
                MovimientoInventarioDetalle(
                    movimiento_inventario=mov, producto_id=i["producto_id"],
                    producto_variante_id=i["producto_variante_id"],
                    ubicacion_origen_id=i["ubicacion_id"] if tipo == TipoMovimiento.SALIDA else None,
                    ubicacion_destino_id=i["ubicacion_id"] if tipo == TipoMovimiento.ENTRADA else None,
                    cantidad=abs(i["cantidad_consumida"]), costo_unitario=Decimal("0"),
                )
                for i in items
            ])
            movimientos.append(mov)

        cambios = [
            {"producto_id": p.pk, "consumido_antes": str(a), "consumido_despues": str(r), "delta": str(d)}
            for p, d, a, r in resumen
        ]
        ip = ua = None
        if request is not None:
            ip = request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR")
            ua = request.META.get("HTTP_USER_AGENT")
        AuditoriaEvento.objects.create(
            empresa=op.empresa, usuario=user if getattr(user, "pk", None) else None,
            modulo="produccion", accion="RECALCULO_CONSUMO_TELA", tabla="consumos_produccion",
            id_registro=str(op.op_id), antes_json={"op_id": op.op_id, "folio_op": op.folio_op},
            despues_json={"op_id": op.op_id, "folio_op": op.folio_op, "cambios": cambios},
            ip=ip, user_agent=ua,
        )
        return {"op": op, "consumo_produccion": consumo, "movimientos": movimientos, "cambios": cambios}
