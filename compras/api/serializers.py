from rest_framework import serializers
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle

class OrdenCompraSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenCompra
        fields = '__all__'
        extra_kwargs = {
            'folio': {'required': False, 'allow_null': True},
            'solicitud_compra': {'required': False, 'allow_null': True},
            'pedido': {'required': False, 'allow_null': True},
            'proveedor': {'required': False, 'allow_null': True},
        }

    def to_internal_value(self, data):
        # Convert "0" or 0 to None for FK fields if they are optional
        for field in ['solicitud_compra', 'pedido', 'proveedor']:
            if field in data and (data[field] == "0" or data[field] == 0):
                data[field] = None
        return super().to_internal_value(data)


class OrdenCompraDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenCompraDetalle
        fields = "__all__"


class OrdenCompraOnboardingHeaderSerializer(serializers.Serializer):
    sucursal = serializers.IntegerField(required=False, allow_null=True)
    proveedor = serializers.IntegerField(required=False, allow_null=True)
    moneda = serializers.IntegerField(required=False, allow_null=True)
    fecha_oc = serializers.DateField(required=False, allow_null=True)
    referencia = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    observaciones = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class OrdenCompraOnboardingDetalleInputSerializer(serializers.Serializer):
    producto = serializers.IntegerField()
    cantidad = serializers.IntegerField(min_value=1)
    precio = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    descripcion = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class OrdenCompraOnboardingSerializer(serializers.Serializer):
    orden_compra_id = serializers.IntegerField(required=False)
    orden_compra = OrdenCompraOnboardingHeaderSerializer(required=False)
    detalle = OrdenCompraOnboardingDetalleInputSerializer(many=True, required=False)


class RecepcionDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecepcionDetalle
        fields = '__all__'
        read_only_fields = ('recepcion',)


class RecepcionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Recepcion
        fields = '__all__'


class RecepcionOnboardingHeaderSerializer(serializers.Serializer):
    orden_compra = serializers.IntegerField()
    almacen = serializers.IntegerField()
    serie_codigo = serializers.CharField(required=False, allow_blank=True)
    fecha_recepcion = serializers.DateTimeField(required=False, allow_null=True)
    remision = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    factura_referencia = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    observaciones = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    transportista = serializers.IntegerField(required=False, allow_null=True)


class RecepcionOnboardingDetalleInputSerializer(serializers.Serializer):
    orden_compra_detalle = serializers.IntegerField()
    cantidad_recibida = serializers.DecimalField(max_digits=18, decimal_places=4)
    ubicacion = serializers.IntegerField(required=False, allow_null=True)
    lote = serializers.IntegerField(required=False, allow_null=True)
    serie = serializers.IntegerField(required=False, allow_null=True)


class RecepcionOnboardingSerializer(serializers.Serializer):
    recepcion = RecepcionOnboardingHeaderSerializer()
    detalle = RecepcionOnboardingDetalleInputSerializer(many=True)

