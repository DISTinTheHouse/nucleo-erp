from rest_framework import serializers
from finanzas.models import Factura, FacturaDetalle

class FacturaDetalleSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)

    class Meta:
        model = FacturaDetalle
        read_only_fields = [
            'factura',
            'precio_unitario',
            'descuento',
            'impuesto',
            'subtotal',
            'total',
            'producto'
        ]
        fields = '__all__'

class FacturaSerializer(serializers.ModelSerializer):
    factura_detalles = FacturaDetalleSerializer(many=True)
    moneda_nombre = serializers.CharField(source='moneda.codigo_iso', read_only=True)
    cliente_nombre = serializers.CharField(source='cliente.nombre', read_only=True)

    class Meta:
        model = Factura
        read_only_fields = [
            'empresa',
            'sucursal',
            'estatus',
            'created_at',
            'updated_at',
            'folio',
            'subtotal',
            'descuento',
            'impuestos',
            'total',
            'cliente',
            'moneda'
        ]
        fields = '__all__'

