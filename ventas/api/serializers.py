from rest_framework import serializers
from ventas.models import Cotizacion, CotizacionDetalle, CotizacionDetalleTalla, Pedido, PedidoDetalle, PedidoDetalleTalla


def _normalize_bordado_config(raw):
    if raw is None:
        return None

    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(",", "\n").splitlines()]
        urls = [p for p in parts if p]
        return {"imagenes": urls}

    if isinstance(raw, list):
        urls = [str(p).strip() for p in raw if str(p).strip()]
        return {"imagenes": urls}

    if isinstance(raw, dict):
        if "imagenes" in raw and isinstance(raw["imagenes"], str):
            parts = [p.strip() for p in raw["imagenes"].replace(",", "\n").splitlines()]
            raw = dict(raw)
            raw["imagenes"] = [p for p in parts if p]
        return raw

    return {"imagenes": [str(raw).strip()]} if str(raw).strip() else {"imagenes": []}

class CotizacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cotizacion
        read_only_fields = ['empresa']
        fields = '__all__'
        extra_kwargs = {
            'oc': {'required': False, 'allow_null': True},
        }

class CotizacionDashboardItemSerializer(serializers.ModelSerializer):
    estatus_label = serializers.CharField(source="get_estatus_display", read_only=True)
    cliente_nombre = serializers.CharField(source="cliente.nombre", read_only=True)
    cliente_razon_social = serializers.CharField(source="cliente.razon_social", read_only=True)
    pedido_id = serializers.IntegerField(read_only=True)
    pedido_folio = serializers.CharField(read_only=True)

    class Meta:
        model = Cotizacion
        fields = [
            "id",
            "estatus",
            "estatus_label",
            "cliente",
            "cliente_nombre",
            "cliente_razon_social",
            "oc",
            "uso_cfdi",
            "gran_total",
            "autorizada_at",
            "cambios_solicitados_at",
            "created_at",
            "updated_at",
            "pedido_id",
            "pedido_folio",
        ]

class CotizacionDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = CotizacionDetalle
        fields = '__all__'

class CotizacionDetalleTallaSerializer(serializers.ModelSerializer):
    class Meta:
        model = CotizacionDetalleTalla
        fields = "__all__"

class CotizacionDetalleWithTallasSerializer(serializers.ModelSerializer):
    tallas = CotizacionDetalleTallaSerializer(many=True, read_only=True)
    class Meta:
        model = CotizacionDetalle
        fields = "__all__"

class CotizacionFullSerializer(serializers.ModelSerializer):
    estatus_label = serializers.CharField(source="get_estatus_display", read_only=True)
    detalles = CotizacionDetalleWithTallasSerializer(source="cotizaciondetalle", many=True, read_only=True)
    cliente_nombre = serializers.CharField(source="cliente.nombre", read_only=True)
    cliente_razon_social = serializers.CharField(source="cliente.razon_social", read_only=True)

    class Meta:
        model = Cotizacion
        fields = "__all__"

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

    def validate(self, attrs):
        lleva_bordado = bool(attrs.get("lleva_bordado"))
        bordado_config = _normalize_bordado_config(attrs.get("bordado_config"))

        if lleva_bordado and bordado_config is None:
            raise serializers.ValidationError({"bordado_config": "Requerido cuando lleva_bordado=true."})

        attrs["bordado_config"] = bordado_config
        return attrs

class PedidoOnboardingDetalleInputSerializer(serializers.Serializer):
    producto = serializers.IntegerField()
    precio_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    costo_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    tallas = PedidoOnboardingTallaInputSerializer(many=True)

class PedidoOnboardingCreateSerializer(serializers.Serializer):
    pedido = PedidoSerializer()
    detalle = PedidoOnboardingDetalleInputSerializer(many=True)

class CotizacionOnboardingTallaInputSerializer(serializers.Serializer):
    talla = serializers.IntegerField()
    cantidad = serializers.IntegerField(min_value=1)
    lleva_bordado = serializers.BooleanField(required=False, default=False)
    bordado_config = serializers.JSONField(required=False, allow_null=True)

    def validate(self, attrs):
        lleva_bordado = bool(attrs.get("lleva_bordado"))
        bordado_config = _normalize_bordado_config(attrs.get("bordado_config"))

        if lleva_bordado and bordado_config is None:
            raise serializers.ValidationError({"bordado_config": "Requerido cuando lleva_bordado=true."})

        attrs["bordado_config"] = bordado_config
        return attrs

class CotizacionOnboardingDetalleInputSerializer(serializers.Serializer):
    producto = serializers.IntegerField()
    precio_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    costo_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    tallas = CotizacionOnboardingTallaInputSerializer(many=True)

class CotizacionOnboardingCreateSerializer(serializers.Serializer):
    cotizacion_id = serializers.IntegerField(required=False)
    cotizacion = CotizacionSerializer()
    detalle = CotizacionOnboardingDetalleInputSerializer(many=True)
