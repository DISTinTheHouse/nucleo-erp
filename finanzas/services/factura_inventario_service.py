"""Reintegro y redescuento de existencia por las piezas de una factura de venta.

Cancelar una factura devuelve sus piezas a existencia; facturarlas de nuevo
las vuelve a descontar. Así el inventario del pedido sigue cuadrando tras
"cancelar y reemitir" (#513).
"""

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from auditoria.models import AuditoriaEvento
from finanzas.exceptions import ErrorDeNegocio
from inventarios.models import (
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    TipoMovimiento,
)

QTY = Decimal("0.0001")


class FacturaInventarioService:
    @staticmethod
    def _pendiente_por_ubicacion(pedido):
        """Neto que el pedido sacó de cada ubicación y aún no regresa, por producto/variante."""
        saldo = defaultdict(lambda: Decimal("0"))
        eventos = AuditoriaEvento.objects.filter(
            modulo="inventarios", tabla="existencias", id_registro=str(pedido.pk)
        ).order_by("id_evento")
        for evento in eventos:
            payload = evento.despues_json or {}
            # ``id_registro`` también lo usan otros flujos con el pk de otra entidad
            # (almacén, recepción, OP, transferencia): solo cuentan los eventos del pedido.
            if payload.get("pedido_id") != pedido.pk:
                continue
            for item in payload.get("items", []):
                if item.get("almacen_id") is None:
                    continue
                llave = (
                    item.get("producto_id"),
                    item.get("producto_variante_id"),
                    item.get("almacen_id"),
                    item.get("ubicacion_id"),
                )
                saldo[llave] -= Decimal(str(item.get("delta", 0)))
        return saldo

    @staticmethod
    def _eventos_de_facturas(pedido, factura_id=None):
        """Neto (delta) por (producto, variante, almacén, ubicación) de los
        reintegros y redescuentos que las facturas hicieron sobre el pedido."""
        neto = defaultdict(lambda: Decimal("0"))
        eventos = AuditoriaEvento.objects.filter(
            modulo="inventarios", tabla="existencias", id_registro=str(pedido.pk)
        ).order_by("id_evento")
        for evento in eventos:
            payload = evento.despues_json or {}
            if payload.get("pedido_id") != pedido.pk or payload.get("factura_id") is None:
                continue
            if factura_id is not None and payload["factura_id"] != factura_id:
                continue
            for item in payload.get("items", []):
                if item.get("almacen_id") is None:
                    continue
                llave = (
                    item.get("producto_id"),
                    item.get("producto_variante_id"),
                    item.get("almacen_id"),
                    item.get("ubicacion_id"),
                )
                neto[llave] += Decimal(str(item.get("delta", 0)))
        return neto

    @staticmethod
    def _piezas(factura):
        piezas = defaultdict(lambda: Decimal("0"))
        detalles = factura.factura_detalles.select_related("pedido_detalle_talla")
        for det in detalles:
            talla = det.pedido_detalle_talla
            variante_id = talla.variante_id if talla else None
            piezas[(det.producto_id, variante_id)] += det.cantidad
        return piezas

    @staticmethod
    def _registrar(factura, pedido, usuario, items, accion, observaciones):
        """Movimiento (si hay usuario) y evento de auditoría de un reintegro o redescuento."""
        referencia = factura.folio or factura.pk
        movimiento = None
        if usuario is not None:
            entrada = accion == "ENTRADA"
            movimiento = MovimientoInventario.objects.create(
                empresa=pedido.empresa,
                sucursal=pedido.sucursal,
                pedido=pedido,
                tipo_movimiento=TipoMovimiento.ENTRADA if entrada else TipoMovimiento.SALIDA,
                usuario=usuario,
                observaciones=f"{observaciones} {referencia}"[:150],
            )
            MovimientoInventarioDetalle.objects.bulk_create(
                MovimientoInventarioDetalle(
                    movimiento_inventario=movimiento,
                    producto_id=e["producto_id"],
                    producto_variante_id=e["producto_variante_id"],
                    **{("ubicacion_destino_id" if entrada else "ubicacion_origen_id"): e["ubicacion_id"]},
                    cantidad=abs(Decimal(e["delta"])),
                    costo_unitario=Decimal("0"),
                )
                for e in items
            )
        AuditoriaEvento.objects.create(
            empresa=pedido.empresa,
            usuario=usuario if getattr(usuario, "pk", None) else None,
            modulo="inventarios",
            accion=accion,
            tabla="existencias",
            id_registro=str(pedido.pk),
            antes_json={"items": items, "pedido_id": pedido.pk, "factura_id": factura.pk},
            despues_json={
                "empresa_id": pedido.empresa_id,
                "sucursal_id": pedido.sucursal_id,
                "pedido_id": pedido.pk,
                "factura_id": factura.pk,
                "items": items,
            },
        )
        return movimiento

    @classmethod
    def redescontar(cls, factura, usuario=None):
        """Vuelve a descontar las piezas que una cancelación regresó a existencia.

        Solo descuenta lo reintegrado por facturas del pedido y no vuelto a
        descontar, de las ubicaciones a donde regresó; la primera factura de un
        pedido no descuenta nada (ya salió al autorizar la cotización). Si ya no
        hay existencia suficiente, falla sin facturar.
        """
        pedido = factura.pedido
        piezas = cls._piezas(factura)
        if pedido is None or not piezas:
            return None

        repuesto = cls._eventos_de_facturas(pedido)
        salidas = []
        for (producto_id, variante_id), restante in piezas.items():
            destinos = sorted(
                (k for k, v in repuesto.items() if k[:2] == (producto_id, variante_id) and v > 0),
                key=lambda k: (-repuesto[k], k[2] or 0, k[3] or 0),
            )
            for llave in destinos:
                if restante <= 0:
                    break
                cantidad = min(repuesto[llave], restante).quantize(QTY, rounding=ROUND_HALF_UP)
                existencia = (
                    Existencia.objects.select_for_update()
                    .filter(
                        producto_id=producto_id,
                        producto_variante_id=variante_id,
                        almacen_id=llave[2],
                        ubicacion_id=llave[3],
                    )
                    .first()
                )
                antes = Decimal(existencia.cantidad or 0) if existencia else Decimal("0")
                if antes < cantidad:
                    raise ErrorDeNegocio(
                        {
                            "inventario": (
                                f"Existencia insuficiente para volver a facturar: producto {producto_id}, "
                                f"requerido {cantidad}, disponible {antes}."
                            )
                        }
                    )
                despues = (antes - cantidad).quantize(QTY, rounding=ROUND_HALF_UP)
                existencia.cantidad = despues
                existencia.stock = max(int(despues), 0)
                existencia.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])
                salidas.append(
                    {
                        "producto_id": producto_id,
                        "producto_variante_id": variante_id,
                        "almacen_id": llave[2],
                        "ubicacion_id": llave[3],
                        "cantidad_before": str(antes),
                        "cantidad_after": str(despues),
                        "delta": str(-cantidad),
                    }
                )
                repuesto[llave] -= cantidad
                restante -= cantidad

        if not salidas:
            return None
        return cls._registrar(
            factura, pedido, usuario, salidas, "SALIDA", "Descuento por refacturar piezas, factura"
        )

    @classmethod
    def reintegrar(cls, factura, usuario=None, solo_redescontado=False):
        """Devuelve a existencia las piezas facturadas; regresa el movimiento o ``None``.

        Solo regresa lo que el pedido descontó y aún no se ha reintegrado, así que
        nunca crea existencia de más (p. ej. si el pedido ya reintegró o se refactura).
        Con ``solo_redescontado`` (Borrador) regresa únicamente lo que esta misma
        factura volvió a descontar al crearse.
        """
        pedido = factura.pedido
        if pedido is None:
            return None

        piezas = cls._piezas(factura)
        if solo_redescontado:
            propio = defaultdict(lambda: Decimal("0"))
            for (producto_id, variante_id, _alm, _ubi), delta in cls._eventos_de_facturas(
                pedido, factura.pk
            ).items():
                propio[(producto_id, variante_id)] -= delta
            piezas = {k: min(v, propio[k]) for k, v in piezas.items() if propio[k] > 0}
        if not piezas:
            return None

        saldo = cls._pendiente_por_ubicacion(pedido)
        entradas = []
        for (producto_id, variante_id), restante in piezas.items():
            destinos = sorted(
                (k for k, v in saldo.items() if k[:2] == (producto_id, variante_id) and v > 0),
                key=lambda k: (-saldo[k], k[2] or 0, k[3] or 0),
            )
            for llave in destinos:
                if restante <= 0:
                    break
                cantidad = min(saldo[llave], restante).quantize(QTY, rounding=ROUND_HALF_UP)
                existencia = (
                    Existencia.objects.select_for_update()
                    .filter(
                        producto_id=producto_id,
                        producto_variante_id=variante_id,
                        almacen_id=llave[2],
                        ubicacion_id=llave[3],
                    )
                    .first()
                ) or Existencia.objects.create(
                    producto_id=producto_id,
                    producto_variante_id=variante_id,
                    almacen_id=llave[2],
                    ubicacion_id=llave[3],
                    cantidad=Decimal("0"),
                    stock=0,
                )
                antes = Decimal(existencia.cantidad or 0)
                despues = (antes + cantidad).quantize(QTY, rounding=ROUND_HALF_UP)
                existencia.cantidad = despues
                existencia.stock = max(int(despues), 0)
                existencia.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])
                entradas.append(
                    {
                        "producto_id": producto_id,
                        "producto_variante_id": variante_id,
                        "almacen_id": llave[2],
                        "ubicacion_id": llave[3],
                        "cantidad_before": str(antes),
                        "cantidad_after": str(despues),
                        "delta": str(cantidad),
                    }
                )
                saldo[llave] -= cantidad
                restante -= cantidad

        if not entradas:
            return None

        return cls._registrar(
            factura, pedido, usuario, entradas, "ENTRADA", "Reintegro por cancelación de factura"
        )
