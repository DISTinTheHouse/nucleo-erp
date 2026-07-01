from decimal import Decimal
from django.db import transaction
from django.core.exceptions import ValidationError
from finanzas.models import Factura, FacturaDetalle
from finanzas.utils.folios import generate_factura_folio


class FacturaService:
    @staticmethod
    @transaction.atomic
    def store_factura(user, validated_data):
        empresa_id = getattr(user, 'empresa', None)
        sucursal_id = getattr(user, 'sucursal_default', None)

        pedido =  validated_data.pop('pedido')
        if not pedido: raise ValidationError({'pedido': 'El pedido no existe'})

        folio_factura = generate_factura_folio(empresa_id, sucursal_id)
        factura_rows = validated_data.pop('factura_detalles', [])
        factura = Factura.objects.create(
            empresa=empresa_id,
            sucursal=sucursal_id,
            cliente=pedido.cliente,
            moneda=pedido.moneda,
            pedido=pedido,
            folio=folio_factura,
            **validated_data
        )

        bulk_data = []
        factura_descuento = Decimal('0.00')
        factura_impuestos = Decimal('0.00')
        factura_subtotal = Decimal('0.00')
        factura_total = Decimal('0.00')
        for factura_row in factura_rows:
            pedido_detalle = factura_row['pedido_detalle']

            cantidad = factura_row['cantidad']
            precio_unitario = pedido_detalle.precio_unitario

            #TODO: ADD descuento, impuesto fields to pedido_detalle
            #porcentaje_descuento = pedido_detalle.descuento or Decimal('0')
            #porcentaje_impuesto = pedido_detalle.impuesto or Decimal('0')
            porcentaje_descuento = Decimal('0')
            porcentaje_impuesto = Decimal('0')

            subtotal = cantidad * precio_unitario

            descuento = subtotal * (porcentaje_descuento / Decimal('100'))
            base_impuesto = subtotal - descuento
            impuesto = base_impuesto * (porcentaje_impuesto / Decimal('100'))
            total = base_impuesto + impuesto
            
            bulk_data.append(
                FacturaDetalle(
                    factura=factura,
                    pedido_detalle=pedido_detalle,
                    producto=pedido_detalle.producto,
                    cantidad=cantidad,
                    descuento=descuento,
                    impuesto=impuesto,
                    precio_unitario=precio_unitario,
                    subtotal=subtotal,
                    total=total
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
        factura.save(
            update_fields=[
                'subtotal',
                'descuento',
                'impuestos',
                'total'
            ])
        return factura
