from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Factura, FacturaDetalle
from finanzas.utils.folios import generate_factura_folio
from ventas.models import PedidoDetalleTalla

CENTAVO = Decimal('0.01')


def _redondear(valor):
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def _precio_de_talla(pedido_detalle_talla):
    """Precio unitario (sin IVA) de una talla: el propio de la talla si lo
    tiene, si no el del renglón del pedido."""
    if pedido_detalle_talla.precio_unitario is not None:
        return pedido_detalle_talla.precio_unitario
    return pedido_detalle_talla.pedido_detalle.precio_unitario or Decimal('0')


class FacturaService:
    """Facturación de pedidos por piezas.

    Un pedido se factura en parcialidades: cada factura toma piezas de tallas
    concretas (``PedidoDetalleTalla``) y nunca más de las pendientes. Quien
    llama debe tener el ``Pedido`` bloqueado (``select_for_update``) para que
    dos facturas simultáneas no tomen las mismas piezas.
    """

    @staticmethod
    def piezas_por_facturar(pedido):
        """Por talla del pedido: piezas pedidas, ya facturadas y pendientes.

        Cuenta como facturado todo renglón de una factura activa no cancelada;
        un Borrador también aparta sus piezas. Los renglones anteriores a la
        facturación por talla (sin ``pedido_detalle_talla``) se descuentan del
        renglón del pedido, repartidos en orden entre sus tallas.
        """
        tallas = list(
            PedidoDetalleTalla.objects.filter(pedido_detalle__pedido=pedido)
            .select_related('pedido_detalle__producto', 'talla')
            .order_by('pedido_detalle_id', 'id')
        )

        facturado_por_talla = defaultdict(Decimal)
        sin_talla_por_detalle = defaultdict(Decimal)
        renglones_facturados = (
            FacturaDetalle.objects.filter(
                pedido_detalle__pedido=pedido, factura__activo=True
            )
            .exclude(factura__estatus=Factura.FacturaStatus.CANCELADA)
            .values('pedido_detalle_id', 'pedido_detalle_talla_id')
            .annotate(cantidad_total=Sum('cantidad'))
        )
        for renglon in renglones_facturados:
            if renglon['pedido_detalle_talla_id'] is None:
                sin_talla_por_detalle[renglon['pedido_detalle_id']] += renglon['cantidad_total']
            else:
                facturado_por_talla[renglon['pedido_detalle_talla_id']] += renglon['cantidad_total']

        piezas = []
        for talla in tallas:
            pedida = Decimal(talla.cantidad)
            facturada = facturado_por_talla[talla.pk]
            pendiente = max(pedida - facturada, Decimal('0'))

            sin_talla = sin_talla_por_detalle[talla.pedido_detalle_id]
            tomar = min(pendiente, sin_talla)
            sin_talla_por_detalle[talla.pedido_detalle_id] -= tomar
            facturada += tomar
            pendiente -= tomar

            piezas.append({
                'pedido_detalle_talla': talla,
                'precio_unitario': _precio_de_talla(talla),
                'cantidad_pedida': pedida,
                'cantidad_facturada': facturada,
                'cantidad_pendiente': pendiente,
            })
        return piezas

    @staticmethod
    @transaction.atomic
    def store_factura(user, validated_data, sucursal):
        """Factura las piezas elegidas: ``factura_detalles`` trae
        ``pedido_detalle_talla`` + ``cantidad`` por renglón."""
        # La sucursal la resuelve y valida quien llama (``_get_default_sucursal``):
        # ``user.sucursal_default`` puede ser None o de otra empresa.
        pedido = validated_data.pop('pedido')
        if not pedido:
            raise ErrorDeNegocio({'pedido': 'El pedido no existe'})

        renglones = validated_data.pop('factura_detalles', [])
        lineas = [
            (renglon['pedido_detalle_talla'], renglon['cantidad'])
            for renglon in renglones
        ]
        return FacturaService._crear_factura(
            getattr(user, 'empresa', None), sucursal, pedido, lineas, **validated_data
        )

    @staticmethod
    @transaction.atomic
    def facturar_pendiente(pedido, empresa, sucursal):
        """Factura todas las piezas que le quedan pendientes al pedido."""
        lineas = [
            (pieza['pedido_detalle_talla'], pieza['cantidad_pendiente'])
            for pieza in FacturaService.piezas_por_facturar(pedido)
            if pieza['cantidad_pendiente'] > 0
        ]
        if not lineas:
            raise ErrorDeNegocio({'pedido': 'El pedido no tiene piezas pendientes por facturar.'})
        return FacturaService._crear_factura(empresa, sucursal, pedido, lineas)

    @staticmethod
    def _validar_lineas(pedido, lineas):
        if not lineas:
            raise ErrorDeNegocio({
                'factura_detalles': 'La factura debe incluir al menos una línea.'
            })

        # Una factura registrada solo por monto (``registrar-pendiente-cobro``)
        # no dice qué piezas cubre: facturar piezas encima la duplicaría.
        hay_factura_por_monto = (
            Factura.objects.filter(pedido=pedido, activo=True, factura_detalles__isnull=True)
            .exclude(estatus=Factura.FacturaStatus.CANCELADA)
            .exists()
        )
        if hay_factura_por_monto:
            raise ErrorDeNegocio({
                'pedido': 'El pedido ya tiene una factura registrada por monto, sin piezas; '
                          'no admite facturación por piezas.'
            })

        pendientes = {
            pieza['pedido_detalle_talla'].pk: pieza
            for pieza in FacturaService.piezas_por_facturar(pedido)
        }
        errores = []
        vistas = set()
        for talla, cantidad in lineas:
            pieza = pendientes.get(talla.pk)
            if pieza is None:
                errores.append(f'La talla {talla.pk} no pertenece al pedido.')
                continue
            if talla.pk in vistas:
                errores.append(f'La talla {talla.pk} viene repetida.')
                continue
            vistas.add(talla.pk)

            if cantidad <= 0 or cantidad != cantidad.to_integral_value():
                errores.append(f'La talla {talla.pk}: la cantidad debe ser un número entero de piezas mayor a 0.')
                continue
            talla_pedida = pieza['pedido_detalle_talla']
            producto = talla_pedida.pedido_detalle.producto
            if producto is None:
                errores.append(
                    f'La talla {talla.pk}: el renglón del pedido no tiene producto de catálogo y no puede facturarse.'
                )
                continue
            if cantidad > pieza['cantidad_pendiente']:
                errores.append(
                    f'{producto.nombre} talla {talla_pedida.talla.nombre}: '
                    f'pendientes {pieza["cantidad_pendiente"]:.0f}, solicitadas {cantidad:.0f}.'
                )
        if errores:
            raise ErrorDeNegocio({'factura_detalles': errores})
        return pendientes

    @staticmethod
    def _crear_factura(empresa, sucursal, pedido, lineas, **campos):
        # Se valida todo antes de generar el folio para no consumirlo en balde.
        pendientes = FacturaService._validar_lineas(pedido, lineas)

        factura = Factura.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            cliente=pedido.cliente,
            moneda=pedido.moneda,
            pedido=pedido,
            folio=generate_factura_folio(empresa, sucursal),
            **campos
        )

        porcentaje_impuesto = Decimal(pedido.iva or 0)
        bulk_data = []
        factura_subtotal = Decimal('0.00')
        factura_descuento = Decimal('0.00')
        factura_impuestos = Decimal('0.00')
        factura_total = Decimal('0.00')
        for talla, cantidad in lineas:
            talla = pendientes[talla.pk]['pedido_detalle_talla']
            precio_unitario = pendientes[talla.pk]['precio_unitario']

            subtotal = _redondear(cantidad * precio_unitario)
            # Los descuentos del pedido quedan fuera de la facturación por piezas.
            descuento = Decimal('0.00')
            impuesto = _redondear((subtotal - descuento) * porcentaje_impuesto / Decimal('100'))
            total = subtotal - descuento + impuesto

            bulk_data.append(
                FacturaDetalle(
                    factura=factura,
                    pedido_detalle=talla.pedido_detalle,
                    pedido_detalle_talla=talla,
                    producto=talla.pedido_detalle.producto,
                    cantidad=cantidad,
                    precio_unitario=precio_unitario,
                    descuento=descuento,
                    porcentaje_impuesto=porcentaje_impuesto,
                    impuesto=impuesto,
                    subtotal=subtotal,
                    total=total,
                )
            )

            factura_subtotal += subtotal
            factura_descuento += descuento
            factura_impuestos += impuesto
            factura_total += total

        FacturaDetalle.objects.bulk_create(bulk_data)
        factura.subtotal = factura_subtotal
        factura.descuento = factura_descuento
        factura.impuestos = factura_impuestos
        factura.total = factura_total
        factura.save(update_fields=['subtotal', 'descuento', 'impuestos', 'total'])
        return factura
