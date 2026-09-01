from decimal import Decimal

from django.db.models import Sum
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from finanzas.models import (
    AlertaMora,
    Banco,
    CentroCosto,
    Cobro,
    CobroDetalle,
    ConciliacionBancaria,
    CuentaBancaria,
    CuentaContable,
    CuentaPorCobrar,
    CuentaPorPagar,
    Factura,
    FacturaDetalle,
    FacturaProveedor,
    FacturaProveedorDetalle,
    MovimientoBancario,
    NotaCredito,
    NotaCreditoDetalle,
    Pago,
    PagoDetalle,
    Poliza,
    PolizaDetalle,
)


class FacturaDesdePedidoInputSerializer(serializers.Serializer):
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
        if abs(expected_total - attrs["total"]) > Decimal("0.01"):
            raise serializers.ValidationError(
                {"total": "El total debe ser igual a subtotal - descuento + impuestos."}
            )
        return attrs


class ConciliacionPrepararInputSerializer(serializers.Serializer):
    cuenta_bancaria = serializers.IntegerField(min_value=1)
    fecha_inicio = serializers.DateField(required=False, allow_null=True)
    fecha_final = serializers.DateField(required=False, allow_null=True)
    saldo_estado_cuenta = serializers.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal("0.00")
    )


class CuentaPorCobrarSerializer(serializers.ModelSerializer):
    cliente_nombre = serializers.CharField(source="cliente.nombre", read_only=True)
    factura_id = serializers.IntegerField(read_only=True)
    factura_folio = serializers.CharField(source="factura.folio", read_only=True)
    moneda_id = serializers.IntegerField(source="factura.moneda_id", read_only=True)
    moneda_codigo = serializers.CharField(source="factura.moneda.codigo_iso", read_only=True)
    empresa_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = CuentaPorCobrar
        fields = [
            "id",
            "empresa_id",
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
    cuenta_contable_id = serializers.IntegerField(read_only=True)
    cuenta_contable_codigo = serializers.CharField(source="cuenta_contable.codigo", read_only=True)
    cuenta_contable_nombre = serializers.CharField(source="cuenta_contable.nombre", read_only=True)
    centro_costo_id = serializers.IntegerField(read_only=True)
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
            if poliza is None:
                continue
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


class CuentaContableSerializer(serializers.ModelSerializer):
    class Meta:
        model = CuentaContable
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
        return attrs


class CentroCostoSerializer(serializers.ModelSerializer):
    class Meta:
        model = CentroCosto
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
        return attrs


class PolizaDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PolizaDetalle
        fields = "__all__"


class PolizaSerializer(serializers.ModelSerializer):
    poliza_detalles = PolizaDetalleSerializer(many=True, required=False, read_only=True)
    total_cargos = serializers.SerializerMethodField()
    total_abonos = serializers.SerializerMethodField()
    cuadre_correcto = serializers.SerializerMethodField()

    class Meta:
        model = Poliza
        fields = "__all__"

    def get_total_cargos(self, obj):
        s = obj.poliza_detalles.aggregate(s=Sum("cargo"))["s"] or 0
        return str(Decimal(str(s)).quantize(Decimal("0.01")))

    def get_total_abonos(self, obj):
        s = obj.poliza_detalles.aggregate(s=Sum("abono"))["s"] or 0
        return str(Decimal(str(s)).quantize(Decimal("0.01")))

    def get_cuadre_correcto(self, obj):
        c = Decimal(str(obj.poliza_detalles.aggregate(s=Sum("cargo"))["s"] or 0))
        a = Decimal(str(obj.poliza_detalles.aggregate(s=Sum("abono"))["s"] or 0))
        return abs(c - a) <= Decimal("0.01")

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            suc = attrs.get("sucursal")
            cc = attrs.get("centro_costo")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
            if suc and emp and getattr(suc, "empresa_id", None) and suc.empresa_id != getattr(emp, "pk", getattr(user_empresa, "pk", None)):
                raise ValidationError({"sucursal": "Sucursal no pertenece a la empresa."})
            if cc and emp and getattr(cc, "empresa_id", None) and cc.empresa_id != getattr(emp, "pk", getattr(user_empresa, "pk", None)):
                raise ValidationError({"centro_costo": "Centro de costo no pertenece a la empresa."})
        return attrs


class FacturaProveedorDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = FacturaProveedorDetalle
        fields = "__all__"


class FacturaProveedorSerializer(serializers.ModelSerializer):
    factura_proveedor_detalles = FacturaProveedorDetalleSerializer(many=True, required=False, read_only=True)
    proveedor_nombre = serializers.CharField(source="proveedor.nombre", read_only=True)
    moneda_codigo = serializers.CharField(source="moneda.codigo_iso", read_only=True)

    class Meta:
        model = FacturaProveedor
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
            emp_id = getattr(emp, "pk", emp) if emp else getattr(user_empresa, "pk", None)
            prov = attrs.get("proveedor")
            if prov and emp_id and getattr(prov, "empresa_id", None) and prov.empresa_id not in (None, emp_id):
                raise ValidationError({"proveedor": "Proveedor no pertenece a la empresa."})
            suc = attrs.get("sucursal")
            if suc and emp_id and getattr(suc, "empresa_id", None) and suc.empresa_id != emp_id:
                raise ValidationError({"sucursal": "Sucursal no pertenece a la empresa."})
            oc = attrs.get("oc")
            if oc and emp_id and getattr(oc, "empresa_id", None) and oc.empresa_id != emp_id:
                raise ValidationError({"oc": "Orden de compra no pertenece a la empresa."})
            recepcion = attrs.get("recepcion")
            if recepcion and oc and getattr(recepcion, "orden_compra_id", None) and recepcion.orden_compra_id != getattr(oc, "pk", oc):
                raise ValidationError({"recepcion": "Recepción no corresponde a la OC."})
        return attrs


class BancoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Banco
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
        return attrs


class CuentaBancariaSerializer(serializers.ModelSerializer):
    banco_nombre = serializers.CharField(source="banco.nombre", read_only=True)
    moneda_codigo = serializers.CharField(source="moneda.codigo_iso", read_only=True)

    class Meta:
        model = CuentaBancaria
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
            emp_id = getattr(emp, "pk", emp) if emp else getattr(user_empresa, "pk", None)
            banco = attrs.get("banco")
            if banco and emp_id and getattr(banco, "empresa_id", None) and banco.empresa_id != emp_id:
                raise ValidationError({"banco": "Banco no pertenece a la empresa."})
        return attrs


class CuentaPorPagarSerializer(serializers.ModelSerializer):
    proveedor_nombre = serializers.CharField(source="proveedor.nombre", read_only=True)
    factura_proveedor_folio = serializers.CharField(source="factura_proveedor.folio", read_only=True)
    moneda_id = serializers.IntegerField(source="factura_proveedor.moneda_id", read_only=True)
    moneda_codigo = serializers.CharField(source="factura_proveedor.moneda.codigo_iso", read_only=True)
    total_pagado = serializers.SerializerMethodField()

    class Meta:
        model = CuentaPorPagar
        fields = "__all__"

    def get_total_pagado(self, obj):
        t = Decimal(str(obj.total or 0)) - Decimal(str(obj.saldo or 0))
        return str(t.quantize(Decimal("0.01")))

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
            emp_id = getattr(emp, "pk", emp) if emp else getattr(user_empresa, "pk", None)
            prov = attrs.get("proveedor")
            if prov and emp_id and getattr(prov, "empresa_id", None) and prov.empresa_id not in (None, emp_id):
                raise ValidationError({"proveedor": "Proveedor no pertenece a la empresa."})
            fp = attrs.get("factura_proveedor")
            if fp and emp_id and getattr(fp, "empresa_id", None) and fp.empresa_id != emp_id:
                raise ValidationError({"factura_proveedor": "Factura de proveedor no pertenece a la empresa."})
        return attrs


class CobroDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = CobroDetalle
        fields = "__all__"


class CobroSerializer(serializers.ModelSerializer):
    cobro_detalles = CobroDetalleSerializer(many=True, required=False)
    cliente_nombre = serializers.CharField(source="cliente.nombre", read_only=True)
    cuenta_bancaria_alias = serializers.CharField(source="cuenta_bancaria.alias", read_only=True)

    class Meta:
        model = Cobro
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
            emp_id = getattr(emp, "pk", emp) if emp else getattr(user_empresa, "pk", None)
            cliente = attrs.get("cliente")
            if cliente and emp_id and getattr(cliente, "empresa_id", None) and cliente.empresa_id != emp_id:
                raise ValidationError({"cliente": "Cliente no pertenece a la empresa."})
            cta = attrs.get("cuenta_bancaria")
            if cta and emp_id and getattr(cta, "empresa_id", None) and cta.empresa_id != emp_id:
                raise ValidationError({"cuenta_bancaria": "Cuenta bancaria no pertenece a la empresa."})
        return attrs


class PagoDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PagoDetalle
        fields = "__all__"


class PagoSerializer(serializers.ModelSerializer):
    pago_detalles = PagoDetalleSerializer(many=True, required=False)
    proveedor_nombre = serializers.CharField(source="proveedor.nombre", read_only=True)
    cuenta_bancaria_alias = serializers.CharField(source="cuenta_bancaria.alias", read_only=True)

    class Meta:
        model = Pago
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
            emp_id = getattr(emp, "pk", emp) if emp else getattr(user_empresa, "pk", None)
            prov = attrs.get("proveedor")
            if prov and emp_id and getattr(prov, "empresa_id", None) and prov.empresa_id not in (None, emp_id):
                raise ValidationError({"proveedor": "Proveedor no pertenece a la empresa."})
            cta = attrs.get("cuenta_bancaria")
            if cta and emp_id and getattr(cta, "empresa_id", None) and cta.empresa_id != emp_id:
                raise ValidationError({"cuenta_bancaria": "Cuenta bancaria no pertenece a la empresa."})
        return attrs


class MovimientoBancarioSerializer(serializers.ModelSerializer):
    cuenta_bancaria_alias = serializers.CharField(source="cuenta_bancaria.alias", read_only=True)

    class Meta:
        model = MovimientoBancario
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            cta = attrs.get("cuenta_bancaria")
            if cta and user_empresa and getattr(cta, "empresa_id", None) and cta.empresa_id != getattr(user_empresa, "pk", None):
                raise ValidationError({"cuenta_bancaria": "Cuenta bancaria no pertenece a la empresa."})
            if attrs.get("cobro") and cta and attrs["cobro"].cuenta_bancaria_id != getattr(cta, "pk", cta):
                raise ValidationError({"cobro": "Cobro corresponde a otra cuenta bancaria."})
            if attrs.get("pago") and cta and attrs["pago"].cuenta_bancaria_id != getattr(cta, "pk", cta):
                raise ValidationError({"pago": "Pago corresponde a otra cuenta bancaria."})
        return attrs


class ConciliacionBancariaSerializer(serializers.ModelSerializer):
    diferencia = serializers.SerializerMethodField()
    cuenta_bancaria_alias = serializers.CharField(source="cuenta_bancaria.alias", read_only=True)

    class Meta:
        model = ConciliacionBancaria
        fields = "__all__"

    def get_diferencia(self, obj):
        return str((Decimal(str(obj.saldo_estado_cuenta or 0)) - Decimal(str(obj.saldo_libros or 0))).quantize(Decimal("0.01")))


class NotaCreditoDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotaCreditoDetalle
        fields = "__all__"


class NotaCreditoSerializer(serializers.ModelSerializer):
    nota_credito_detalles = NotaCreditoDetalleSerializer(many=True, required=False, read_only=True)
    cliente_nombre = serializers.CharField(source="cliente.nombre", read_only=True)
    factura_folio = serializers.CharField(source="factura.folio", read_only=True)

    class Meta:
        model = NotaCredito
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            factura = attrs.get("factura")
            cliente = attrs.get("cliente")
            if factura and user_empresa and getattr(factura, "empresa_id", None) and factura.empresa_id != getattr(user_empresa, "pk", None):
                raise ValidationError({"factura": "Factura no pertenece a la empresa."})
            if cliente and factura and cliente.pk != getattr(factura, "cliente_id", None):
                raise ValidationError({"cliente": "Cliente no coincide con la factura."})
        return attrs


class AlertaMoraSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertaMora
        fields = "__all__"

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
        return attrs
