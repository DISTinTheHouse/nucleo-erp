from decimal import Decimal, InvalidOperation
from rest_framework import serializers
from ventas.models import Cotizacion, CotizacionDetalle, CotizacionDetalleTalla, Pedido, PedidoDetalle, PedidoDetalleTalla

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
    piezas = serializers.SerializerMethodField()
    importe_sin_iva = serializers.SerializerMethodField()

    def get_piezas(self, obj):
        piezas = getattr(obj, "piezas", None)
        if piezas is not None:
            try:
                return int(piezas)
            except Exception:
                return 0
        total = 0
        try:
            detalles = obj.cotizaciondetalle.all()
        except Exception:
            detalles = []
        for det in detalles:
            try:
                tallas = det.tallas.all()
            except Exception:
                tallas = []
            for t in tallas:
                total += int(getattr(t, "cantidad", 0) or 0)
        return total

    def get_importe_sin_iva(self, obj):
        gran_total = getattr(obj, "gran_total", None)
        iva = getattr(obj, "iva", None)
        try:
            iva_int = int(iva or 0)
        except Exception:
            iva_int = 0
        if gran_total in (None, ""):
            base = Decimal("0")
        else:
            try:
                base = Decimal(str(gran_total))
            except (InvalidOperation, TypeError, ValueError):
                base = Decimal("0")
        if iva_int <= 0:
            return base
        factor = Decimal("1") + (Decimal(iva_int) / Decimal("100"))
        if factor == 0:
            return base
        try:
            return (base / factor).quantize(Decimal("0.01"))
        except Exception:
            return base / factor

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
            "importe_sin_iva",
            "piezas",
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
    piezas = serializers.SerializerMethodField()
    importe_sin_iva = serializers.SerializerMethodField()

    def get_piezas(self, obj):
        piezas = getattr(obj, "piezas", None)
        if piezas is not None:
            try:
                return int(piezas)
            except Exception:
                return 0
        total = 0
        try:
            detalles = obj.cotizaciondetalle.all()
        except Exception:
            detalles = []
        for det in detalles:
            try:
                tallas = det.tallas.all()
            except Exception:
                tallas = []
            for t in tallas:
                total += int(getattr(t, "cantidad", 0) or 0)
        return total

    def get_importe_sin_iva(self, obj):
        gran_total = getattr(obj, "gran_total", None)
        iva = getattr(obj, "iva", None)
        try:
            iva_int = int(iva or 0)
        except Exception:
            iva_int = 0
        if gran_total in (None, ""):
            base = Decimal("0")
        else:
            try:
                base = Decimal(str(gran_total))
            except (InvalidOperation, TypeError, ValueError):
                base = Decimal("0")
        if iva_int <= 0:
            return base
        factor = Decimal("1") + (Decimal(iva_int) / Decimal("100"))
        if factor == 0:
            return base
        try:
            return (base / factor).quantize(Decimal("0.01"))
        except Exception:
            return base / factor

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

class CotizacionOnboardingDetalleInputSerializer(serializers.Serializer):
    producto = serializers.IntegerField()
    precio_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    costo_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    tallas = CotizacionOnboardingTallaInputSerializer(many=True)

class CotizacionOnboardingCreateSerializer(serializers.Serializer):
    cotizacion_id = serializers.IntegerField(required=False)
    cotizacion = CotizacionSerializer()
    detalle = CotizacionOnboardingDetalleInputSerializer(many=True)
