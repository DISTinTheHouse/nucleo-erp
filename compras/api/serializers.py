from rest_framework import serializers
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle


class OrdenCompraDetalleReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenCompraDetalle
        fields = ['producto_id', 'descripcion', 'cantidad', 'descuento', 'importe', 'piezas', 'precio']


class OrdenCompraSerializer(serializers.ModelSerializer):
    estatus_label = serializers.SerializerMethodField()
    proveedor_nombre = serializers.SerializerMethodField()

    class Meta:
        model = OrdenCompra
        fields = [
            'id',
            'empresa',
            'sucursal',
            'proveedor',
            'proveedor_nombre',
            'solicitud_compra',
            'moneda',
            'usuario',
            'pedido',
            'folio',
            'referencia',
            'fecha_oc',
            'fecha_entrega_estimada',
            'fecha_autorizacion',
            'fecha_vencimiento',
            'estatus',
            'estatus_label',
            'subtotal',
            'descuento',
            'impuestos',
            'total',
            'tipo',
            'total_piezas',
            'flete',
            'seguros',
            'porcentaje_iva',
            'total_iva',
            'gran_total',
            'a_cuenta',
            'observaciones',
            'activo',
            'created_at',
            'updated_at',
        ]
        extra_kwargs = {
            'folio': {'required': False, 'allow_null': True},
            'solicitud_compra': {'required': False, 'allow_null': True},
            'pedido': {'required': False, 'allow_null': True},
            'proveedor': {'required': False, 'allow_null': True},
        }

    def get_estatus_label(self, obj):
        return obj.get_estatus_display()

    def get_proveedor_nombre(self, obj):
        return obj.proveedor.nombre if obj.proveedor_id else None

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


class OrdenCompraRetrieveSerializer(OrdenCompraSerializer):
    detalles = OrdenCompraDetalleReadSerializer(many=True, read_only=True, source='ordencompradetalle_set')

    class Meta(OrdenCompraSerializer.Meta):
        fields = OrdenCompraSerializer.Meta.fields + ['detalles']


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
    detalles = OrdenCompraOnboardingDetalleInputSerializer(many=True, required=False)


class RecepcionDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecepcionDetalle
        fields = (
            "id",
            "recepcion",
            "orden_compra_detalle",
            "producto",
            "ubicacion",
            "lote",
            "serie",
            "cantidad_recibida",
        )
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

