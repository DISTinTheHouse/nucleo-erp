from decimal import Decimal

from rest_framework import serializers
from finanzas.models import CuentaPorCobrar, Factura, FacturaDetalle, PolizaDetalle


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


class PolizaDetalleRelacionadoSerializer(serializers.ModelSerializer):
    cuenta_contable_id = serializers.IntegerField(source="cuenta_contable_id", read_only=True)
    cuenta_contable_codigo = serializers.CharField(source="cuenta_contable.codigo", read_only=True)
    cuenta_contable_nombre = serializers.CharField(source="cuenta_contable.nombre", read_only=True)
    centro_costo_id = serializers.IntegerField(source="centro_costo_id", read_only=True)
    centro_costo_nombre = serializers.CharField(source="centro_costo.nombre", read_only=True)

    class Meta:
        model = PolizaDetalle
        fields = [
            "id",
            "cuenta_contable_id",
            "cuenta_contable_codigo",
            "cuenta_contable_nombre",
            "centro_costo_id",
            "centro_costo_nombre",
            "cargo",
            "abono",
            "referencia",
            "observaciones",
            "orden",
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


class CuentaPorCobrarDetalleSerializer(CuentaPorCobrarSerializer):
    factura = FacturaSerializer(read_only=True)
    total_pagado = serializers.SerializerMethodField()
    polizas = serializers.SerializerMethodField()

    class Meta(CuentaPorCobrarSerializer.Meta):
        fields = CuentaPorCobrarSerializer.Meta.fields + [
            "factura",
            "total_pagado",
            "polizas",
        ]

    def get_total_pagado(self, obj):
        total = Decimal(str(obj.total or 0))
        saldo = Decimal(str(obj.saldo or 0))
        return str((total - saldo).quantize(Decimal("0.01")))

    def get_polizas(self, obj):
        detalles = (
            PolizaDetalle.objects.filter(factura=obj.factura)
            .select_related("poliza", "cuenta_contable", "centro_costo")
            .order_by("poliza_id", "orden", "id")
        )
        polizas_map = {}
        for detalle in detalles:
            poliza = detalle.poliza
            if poliza.pk not in polizas_map:
                polizas_map[poliza.pk] = {
                    "id": poliza.pk,
                    "folio": poliza.folio,
                    "tipo": poliza.tipo,
                    "fecha": poliza.fecha,
                    "concepto": poliza.concepto,
                    "estatus": poliza.estatus,
                    "total_cargos": Decimal("0.00"),
                    "total_abonos": Decimal("0.00"),
                    "detalles": [],
                }

            row = polizas_map[poliza.pk]
            row["total_cargos"] += Decimal(str(detalle.cargo or 0))
            row["total_abonos"] += Decimal(str(detalle.abono or 0))
            row["detalles"].append(PolizaDetalleRelacionadoSerializer(detalle).data)

        for row in polizas_map.values():
            row["total_cargos"] = str(row["total_cargos"].quantize(Decimal("0.01")))
            row["total_abonos"] = str(row["total_abonos"].quantize(Decimal("0.01")))

        return list(polizas_map.values())

