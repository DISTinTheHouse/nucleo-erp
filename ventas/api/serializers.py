from rest_framework import serializers
from ventas.models import Cotizacion, CotizacionDetalle, Pedido, PedidoDetalle, PedidoDetalleTalla

class CotizacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cotizacion
        read_only_fields = ['empresa']
        fields = '__all__'

class CotizacionDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = CotizacionDetalle
        fields = '__all__'

class PedidoSerializer(serializers.ModelSerializer):
    folio = serializers.CharField(read_only=True)
    folio_consecutivo = serializers.IntegerField(read_only=True)
    class Meta:
        model = Pedido
        read_only_fields = ['empresa']
        fields = '__all__'
        extra_kwargs = {
            'cotizacion': {'required': False, 'allow_null': True},
        }

class PedidoDetalleSerializer(serializers.ModelSerializer):
    pedido_folio = serializers.CharField(source='pedido.folio', read_only=True)
    class Meta:
        model = PedidoDetalle
        fields = '__all__'

class PedidoDetalleTallaSerializer(serializers.ModelSerializer):
    pedido_folio = serializers.CharField(source='pedido_detalle.pedido.folio', read_only=True)
    class Meta:
        model = PedidoDetalleTalla
        fields = '__all__'

class PedidoDetalleWithTallasSerializer(serializers.ModelSerializer):
    pedido_folio = serializers.CharField(source='pedido.folio', read_only=True)
    tallas = PedidoDetalleTallaSerializer(many=True, read_only=True)
    class Meta:
        model = PedidoDetalle
        fields = '__all__'

class PedidoOnboardingTallaInputSerializer(serializers.Serializer):
    talla = serializers.IntegerField()
    cantidad = serializers.IntegerField(min_value=1)
    lleva_bordado = serializers.BooleanField(required=False, default=False)
    bordado_config = serializers.JSONField(required=False, allow_null=True)

class PedidoOnboardingDetalleInputSerializer(serializers.Serializer):
    producto = serializers.IntegerField()
    precio_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    costo_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    tallas = PedidoOnboardingTallaInputSerializer(many=True)

class PedidoOnboardingCreateSerializer(serializers.Serializer):
    pedido = PedidoSerializer()
    detalle = PedidoOnboardingDetalleInputSerializer(many=True)
