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
    correo_facturas = serializers.SerializerMethodField()

    def get_correo_facturas(self, obj):
        """
        Correo de facturación resuelto en lectura (read-only), sin columna nueva.

        Mismo nombre y semántica que `Pedido.correo_facturas`, de modo que el
        frontend lee el correo de facturación con la misma clave en el Pedido y
        en la Factura. Se resuelve por prioridad:

          1. `pedido.correo_facturas` — correo capturado en el Pedido de origen.
             Es el "correo al momento de facturar": puede diverger del catálogo
             del cliente porque se edita por pedido. Refleja la snapshot que ya
             vive en el Pedido, sin duplicar el dato en la Factura.
          2. `cliente.correo` — respaldo desde el catálogo del cliente. Necesario
             porque `Factura.pedido` es nullable (p. ej. facturas creadas vía
             `registrar-pendiente-cobro` sin pedido) o el pedido puede no traer
             correo.
          3. `None` — no hay correo disponible. Se devuelve null (no cadena
             vacía) para que el frontend distinga "sin correo" de un correo
             válido.
        """
        pedido = getattr(obj, 'pedido', None)
        if pedido is not None:
            correo_pedido = (pedido.correo_facturas or '').strip()
            if correo_pedido:
                return correo_pedido
        cliente = getattr(obj, 'cliente', None)
        if cliente is not None:
            correo_cliente = (cliente.correo or '').strip()
            if correo_cliente:
                return correo_cliente
        return None

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

