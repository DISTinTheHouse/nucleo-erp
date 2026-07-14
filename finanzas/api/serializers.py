from decimal import Decimal

from rest_framework import serializers
from finanzas.models import CuentaPorCobrar, Factura, FacturaDetalle


class FacturaDesdePedidoInputSerializer(serializers.Serializer):
    """
    Entrada del endpoint `facturas/desde-pedido/`.
    Solo recibe el id del Pedido; el detalle de la factura se arma
    íntegramente en el servidor a partir del Pedido (facturación total).
    """
    pedido = serializers.IntegerField(min_value=1)


class FacturaPendienteCobroInputSerializer(serializers.Serializer):
    cliente = serializers.IntegerField(min_value=1)
    moneda = serializers.IntegerField(min_value=1)
    pedido = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    folio = serializers.CharField(max_length=30, required=False, allow_blank=True)
    fecha_vencimiento = serializers.DateField(required=False, allow_null=True)
    subtotal = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0"))
    descuento = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=Decimal("0"),
        required=False,
        default=Decimal("0.00"),
    )
    impuestos = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=Decimal("0"),
        required=False,
        default=Decimal("0.00"),
    )
    total = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0.01"))
    referencia = serializers.CharField(max_length=100, required=False, allow_blank=True)
    observaciones = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        expected_total = attrs["subtotal"] - attrs["descuento"] + attrs["impuestos"]
        if expected_total != attrs["total"]:
            raise serializers.ValidationError(
                {"total": "El total debe ser igual a subtotal - descuento + impuestos."}
            )
        return attrs


class CuentaPorCobrarSerializer(serializers.ModelSerializer):
    cliente_nombre = serializers.CharField(source="cliente.nombre", read_only=True)
    factura_id = serializers.IntegerField(read_only=True)
    factura_folio = serializers.CharField(source="factura.folio", read_only=True)
    moneda_id = serializers.IntegerField(source="factura.moneda_id", read_only=True)
    moneda_codigo = serializers.CharField(source="factura.moneda.codigo_iso", read_only=True)

    class Meta:
        model = CuentaPorCobrar
        fields = [
            "id",
            "cliente",
            "cliente_nombre",
            "factura_id",
            "factura_folio",
            "moneda_id",
            "moneda_codigo",
            "fecha_emision",
            "fecha_vencimiento",
            "total",
            "saldo",
            "estatus",
            "referencia",
            "fecha_ultimo_pago",
            "observaciones",
            "created_at",
            "updated_at",
        ]


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

