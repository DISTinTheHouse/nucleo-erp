from decimal import Decimal, InvalidOperation
from rest_framework import serializers
from ventas.models import (
    Cotizacion,
    CotizacionDetalle,
    CotizacionDetalleTalla,
    CotizacionServicioExtra,
    Pedido,
    PedidoDetalle,
    PedidoDetalleTalla,
    PedidoServicioExtra,
)

class CotizacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cotizacion
        read_only_fields = ['empresa']
        fields = '__all__'
        extra_kwargs = {
            'oc': {'required': False, 'allow_null': True},
            'persona_pagos': {'required': False, 'allow_null': True, 'allow_blank': True},
            'correo_facturas': {'required': False, 'allow_null': True, 'allow_blank': True},
            'telefono_pagos': {'required': False, 'allow_null': True, 'allow_blank': True},
            'cliente': {'required': False, 'allow_null': True},
            'moneda': {'required': False, 'allow_null': True},
            'forma_pago': {'required': False, 'allow_null': True, 'allow_blank': True},
            'metodo_pago': {'required': False, 'allow_null': True, 'allow_blank': True},
            'uso_cfdi': {'required': False, 'allow_null': True, 'allow_blank': True},
        }

class CotizacionDashboardItemSerializer(serializers.ModelSerializer):
    estatus_label = serializers.CharField(source="get_estatus_display", read_only=True)
    tipo_pedido_label = serializers.CharField(
        source="get_tipo_pedido_display", read_only=True
    )
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
            "tipo_pedido",
            "tipo_pedido_label",
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
    producto_nombre = serializers.CharField(source="producto.nombre", read_only=True)
    color_nombre = serializers.CharField(source="color.nombre", read_only=True, default=None)
    color_codigo_hex = serializers.CharField(source="color.codigo_hex", read_only=True, default=None)

    class Meta:
        model = CotizacionDetalle
        fields = '__all__'

class CotizacionDetalleTallaSerializer(serializers.ModelSerializer):
    talla_nombre = serializers.CharField(source="talla.nombre", read_only=True)

    class Meta:
        model = CotizacionDetalleTalla
        fields = "__all__"

class CotizacionDetalleWithTallasSerializer(serializers.ModelSerializer):
    tallas = CotizacionDetalleTallaSerializer(many=True, read_only=True)
    producto_nombre = serializers.CharField(source="producto.nombre", read_only=True)
    color_nombre = serializers.CharField(source="color.nombre", read_only=True, default=None)
    color_codigo_hex = serializers.CharField(source="color.codigo_hex", read_only=True, default=None)

    class Meta:
        model = CotizacionDetalle
        fields = "__all__"

class CotizacionServicioExtraSerializer(serializers.ModelSerializer):
    class Meta:
        model = CotizacionServicioExtra
        fields = "__all__"

class CotizacionFullSerializer(serializers.ModelSerializer):
    estatus_label = serializers.CharField(source="get_estatus_display", read_only=True)
    detalles = CotizacionDetalleWithTallasSerializer(source="cotizaciondetalle", many=True, read_only=True)
    servicios_extras = CotizacionServicioExtraSerializer(many=True, read_only=True)
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

class PedidoDetalleTallaReadSerializer(serializers.ModelSerializer):
    """Talla anidada de un renglón de pedido (solo lectura).

    Resuelve los nombres a través de las FK que el ``prefetch_related`` del
    viewset ya trae con ``select_related`` — sin consultas por fila, misma
    convención que ``TransferenciaDetalleReadSerializer`` en WMS.

    ``variante`` (``ProductoVariante``) es opcional por talla; cuando falta,
    ``variante_nombre``/``variante_sku`` quedan en ``null``. Como
    ``ProductoVariante.nombre`` puede venir vacío, ``variante_nombre`` cae al
    ``sku`` (que es único y siempre existe).
    """

    talla_nombre = serializers.CharField(source="talla.nombre", read_only=True)
    variante_nombre = serializers.SerializerMethodField()
    variante_sku = serializers.CharField(source="variante.sku", read_only=True, default=None)

    class Meta:
        model = PedidoDetalleTalla
        fields = "__all__"

    def get_variante_nombre(self, obj):
        if not obj.variante_id:
            return None
        return obj.variante.nombre or obj.variante.sku

class PedidoDetalleReadSerializer(serializers.ModelSerializer):
    """Renglón anidado (``detalles``) de un pedido (solo lectura).

    A diferencia de Transferencias/Picking en WMS, ``PedidoDetalle`` NO tiene
    ``producto_variante`` ni ``cantidad`` propios: la variante y la cantidad
    viven por talla (``PedidoDetalleTalla``), así que aquí se anidan las
    ``tallas`` y se agrega ``cantidad_total`` como suma de sus cantidades ya
    prefetcheadas — mismo criterio que ``get_piezas`` en cotizaciones.
    """

    producto_nombre = serializers.CharField(source="producto.nombre", read_only=True)
    color_nombre = serializers.CharField(source="color.nombre", read_only=True, default=None)
    color_codigo_hex = serializers.CharField(source="color.codigo_hex", read_only=True, default=None)
    tallas = PedidoDetalleTallaReadSerializer(many=True, read_only=True)
    cantidad_total = serializers.SerializerMethodField()

    class Meta:
        model = PedidoDetalle
        fields = "__all__"

    def get_cantidad_total(self, obj):
        return sum(int(t.cantidad or 0) for t in obj.tallas.all())

class PedidoSerializer(serializers.ModelSerializer):
    folio = serializers.CharField(read_only=True)
    folio_consecutivo = serializers.IntegerField(read_only=True)
    servicios_extras = serializers.SerializerMethodField()
    # Solo lectura: no cambia el contrato de escritura de ningún endpoint de
    # Pedido (POST/PATCH ignoran ``detalles``).
    detalles = PedidoDetalleReadSerializer(many=True, read_only=True)

    def get_servicios_extras(self, obj):
        try:
            qs = obj.servicios_extras.all().order_by("id")
        except Exception:
            return []
        return PedidoServicioExtraSerializer(qs, many=True).data

    class Meta:
        model = Pedido
        read_only_fields = ['empresa']
        fields = '__all__'
        extra_kwargs = {
            'cotizacion': {'required': False, 'allow_null': True},
        }

class PedidoServicioExtraSerializer(serializers.ModelSerializer):
    class Meta:
        model = PedidoServicioExtra
        fields = "__all__"

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
    lleva_reflejante = serializers.BooleanField(required=False, default=False)
    reflejante_config = serializers.JSONField(required=False, allow_null=True)
    lleva_corte_manga = serializers.BooleanField(required=False, default=False)
    corte_manga_config = serializers.JSONField(required=False, allow_null=True)
    lleva_cambio_talla = serializers.BooleanField(required=False, default=False)
    cambio_talla_config = serializers.JSONField(required=False, allow_null=True)
    lleva_serigrafia = serializers.BooleanField(required=False, default=False)
    serigrafia_config = serializers.JSONField(required=False, allow_null=True)

class PedidoOnboardingDetalleInputSerializer(serializers.Serializer):
    producto = serializers.IntegerField()
    precio_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    costo_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    tallas = PedidoOnboardingTallaInputSerializer(many=True)

class ServicioExtraInputSerializer(serializers.Serializer):
    nombre = serializers.CharField(max_length=150)
    monto = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0)
    cantidad = serializers.IntegerField(min_value=1, required=False, default=1)
    visible_en_factura = serializers.BooleanField(required=False, default=True)

class PedidoOnboardingCreateSerializer(serializers.Serializer):
    pedido = PedidoSerializer()
    detalle = PedidoOnboardingDetalleInputSerializer(many=True)
    servicios_extras = ServicioExtraInputSerializer(many=True, required=False)

class CotizacionOnboardingTallaInputSerializer(serializers.Serializer):
    talla = serializers.IntegerField()
    cantidad = serializers.IntegerField(min_value=1)
    lleva_bordado = serializers.BooleanField(required=False, default=False)
    bordado_config = serializers.JSONField(required=False, allow_null=True)
    lleva_reflejante = serializers.BooleanField(required=False, default=False)
    reflejante_config = serializers.JSONField(required=False, allow_null=True)
    lleva_corte_manga = serializers.BooleanField(required=False, default=False)
    corte_manga_config = serializers.JSONField(required=False, allow_null=True)
    lleva_cambio_talla = serializers.BooleanField(required=False, default=False)
    cambio_talla_config = serializers.JSONField(required=False, allow_null=True)
    lleva_serigrafia = serializers.BooleanField(required=False, default=False)
    serigrafia_config = serializers.JSONField(required=False, allow_null=True)

class CotizacionOnboardingDetalleInputSerializer(serializers.Serializer):
    producto = serializers.IntegerField()
    color = serializers.IntegerField(required=False, allow_null=True)
    color_id = serializers.IntegerField(required=False, allow_null=True)
    direccion_envio_cliente = serializers.IntegerField(required=False, allow_null=True)
    direccion_envio = serializers.IntegerField(required=False, allow_null=True)
    precio_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    costo_unitario = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    tallas = CotizacionOnboardingTallaInputSerializer(many=True)

class CotizacionOnboardingCreateSerializer(serializers.Serializer):
    cotizacion_id = serializers.IntegerField(required=False)
    cotizacion = CotizacionSerializer()
    detalle = CotizacionOnboardingDetalleInputSerializer(many=True)
    servicios_extras = ServicioExtraInputSerializer(many=True, required=False)
