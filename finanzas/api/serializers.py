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
from finanzas.services.cuenta_por_pagar_service import (
    CuentaPorPagarService,
    DUPLICATE_INVOICE_MESSAGE,
)


class EmpresaResueltaEnServidorMixin:
    """``empresa`` la fija el servidor, no el cliente.

    Estos serializers usan ``fields = "__all__"`` sobre modelos cuya FK
    ``empresa`` es NOT NULL, así que DRF la generaba como
    ``PrimaryKeyRelatedField(required=True)``. Como ``is_valid()`` corre ANTES de
    ``perform_create()``, un alta sin ``empresa`` moría en la validación del
    serializer con ``{"empresa": ["Este campo es requerido."]}`` y el
    ``_resolve_empresa()`` del ViewSet —que ya sabe deducirla de ``user.empresa``—
    nunca llegaba a ejecutarse. Era código muerto en la ruta de create.

    Reglas, decididas con el equipo:

    - **Update (PUT/PATCH): de sólo lectura para TODOS**, superusuario incluido.
      Reasignar el tenant por PATCH arrastraba facturas, CxC, pólizas y
      movimientos a otra empresa sin ninguna validación de consistencia; hoy eso
      era posible para el superusuario (medido: HTTP 200 moviendo un banco de la
      empresa 1 a la 2).
    - **Create: de sólo lectura salvo para el superusuario**, que la sigue
      mandando explícitamente. Es la capacidad que ``_resolve_empresa()``
      documenta ("puede crear para cualquier empresa") y que un ``read_only``
      incondicional habría eliminado.

    Para el resto de usuarios el campo desaparece de la entrada, así que
    ``_resolve_empresa()`` cae en ``user.empresa``. El aislamiento no se relaja:
    lo que el cliente mande ya no se usa, y ``perform_create()`` sigue pasando la
    empresa resuelta a ``serializer.save(empresa=...)``.

    ``empresa`` sigue apareciendo en la RESPUESTA: los campos de sólo lectura se
    serializan igual. Es un cambio de escritura, no de shape.
    """

    def _empresa_es_escribible(self):
        # En update ``self.instance`` es el objeto que se edita. En list, DRF
        # instancia el hijo del ``ListSerializer`` sin instancia, pero ahí sólo
        # se serializa: que el campo quede escribible no tiene efecto.
        if self.instance is not None:
            return False
        user = getattr(self.context.get("request"), "user", None)
        return bool(getattr(user, "is_superuser", False))

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()
        if not self._empresa_es_escribible():
            kwargs = dict(extra_kwargs.get("empresa", {}))
            kwargs["read_only"] = True
            # ``read_only`` y ``required`` son incompatibles en DRF.
            kwargs.pop("required", None)
            extra_kwargs["empresa"] = kwargs
        return extra_kwargs


class CreateOnlyNestedLinesMixin:
    """Líneas anidadas escribibles solo en create; en update quedan read-only."""

    nested_write_on_create_only = ()

    def get_fields(self):
        fields = super().get_fields()
        if self.instance is not None:
            for name in self.nested_write_on_create_only:
                field = fields.get(name)
                if field is None:
                    continue
                field.read_only = True
                field.required = False
        return fields


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


class CuentaContableSerializer(EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
    class Meta:
        model = CuentaContable
        fields = "__all__"
        # DRF deriva un ``UniqueTogetherValidator`` de
        # ``uq_cuenta_contable_empresa_codigo``, pero sólo corre cuando el cliente
        # manda ``empresa`` --el superusuario-- y reporta en ``non_field_errors``,
        # ignorando además la condición del código en blanco. Se desactiva para
        # que la unicidad hable siempre por ``codigo``, la mande quien la mande:
        # de eso se encarga ``_validar_codigo_unico_en_la_empresa``.
        validators = []

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
        self._validar_codigo_unico_en_la_empresa(attrs)
        return attrs

    def _validar_codigo_unico_en_la_empresa(self, attrs):
        """Devuelve un 400 por campo donde la constraint daría un 500.

        ``uq_cuenta_contable_empresa_codigo`` es la garantía real; esto es su
        mensaje. No se usa ``UniqueTogetherValidator``: ``empresa`` es de sólo
        lectura para el usuario normal (``EmpresaResueltaEnServidorMixin``), así
        que no llega en ``attrs`` y el validador de DRF la exigiría como campo
        escribible --el alcance saldría del cuerpo de la petición, que es justo
        lo que el aislamiento por empresa no permite. La empresa se toma de la
        misma fuente que ``perform_create``: la del superusuario cuando la manda
        explícitamente, si no la del usuario; y en una edición, la de la fila.
        """
        codigo = attrs.get("codigo", getattr(self.instance, "codigo", "") or "")
        # El código en blanco no es identidad: repetirlo es válido, igual que en
        # la condición de la constraint.
        if not codigo:
            return
        empresa_id = self._empresa_del_servidor(attrs)
        if empresa_id is None:
            return
        gemelas = CuentaContable.objects.filter(empresa_id=empresa_id, codigo=codigo)
        if self.instance is not None:
            gemelas = gemelas.exclude(pk=self.instance.pk)
        if gemelas.exists():
            raise ValidationError(
                {"codigo": "Ya existe una cuenta contable con este código en la empresa."}
            )

    def _empresa_del_servidor(self, attrs):
        if self.instance is not None:
            return self.instance.empresa_id
        emp = attrs.get("empresa")
        if emp is not None:
            return getattr(emp, "pk", emp)
        user = getattr(self.context.get("request"), "user", None)
        return getattr(getattr(user, "empresa", None), "pk", None)


class CentroCostoSerializer(EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
    class Meta:
        model = CentroCosto
        fields = "__all__"
        # DRF deriva un ``UniqueTogetherValidator`` de
        # ``uq_centro_costo_empresa_codigo``, pero sólo corre cuando el cliente
        # manda ``empresa`` --el superusuario-- y reporta en ``non_field_errors``,
        # ignorando además la condición del código en blanco. Se desactiva para
        # que la unicidad hable siempre por ``codigo``, la mande quien la mande:
        # de eso se encarga ``_validar_codigo_unico_en_la_empresa``. Ojo: vaciar
        # ``validators`` apaga TODOS los derivados, no sólo ése. Un segundo campo
        # único que se agregue al modelo no se validará aquí y su violación
        # saldrá como 500 hasta que se le escriba su propia comprobación.
        validators = []

    def validate(self, attrs):
        req = self.context.get("request")
        if req and hasattr(req, "user"):
            user_empresa = getattr(req.user, "empresa", None)
            emp = attrs.get("empresa")
            if user_empresa and emp and getattr(emp, "pk", emp) != getattr(user_empresa, "pk", user_empresa):
                raise ValidationError({"empresa": "Empresa no autorizada."})
        self._validar_codigo_unico_en_la_empresa(attrs)
        return attrs

    def _validar_codigo_unico_en_la_empresa(self, attrs):
        """Devuelve un 400 por campo donde la constraint daría un 500.

        Mismo criterio que ``CuentaContableSerializer``:
        ``uq_centro_costo_empresa_codigo`` es la garantía real y esto es su
        mensaje. No se usa ``UniqueTogetherValidator`` porque ``empresa`` es de
        sólo lectura para el usuario normal (``EmpresaResueltaEnServidorMixin``),
        así que no llega en ``attrs`` y el validador de DRF la exigiría como campo
        escribible --el alcance saldría del cuerpo de la petición, que es justo lo
        que el aislamiento por empresa no permite. La empresa se toma de la misma
        fuente que ``perform_create``: la del superusuario cuando la manda
        explícitamente, si no la del usuario; y en una edición, la de la fila.
        """
        codigo = attrs.get("codigo", getattr(self.instance, "codigo", "") or "")
        # El código en blanco no es identidad: repetirlo es válido, igual que en
        # la condición de la constraint.
        if not codigo:
            return
        # Y una fila dada de baja tampoco reserva su código: se compara el estatus
        # con el que la fila QUEDARÁ, no con el que tenía. Así, reactivar por PATCH
        # una baja cuyo código ya ocupa otra activa se rechaza aquí en vez de
        # reventar contra la constraint, y darla de alta inactiva no disputa nada.
        activo = attrs.get("activo", getattr(self.instance, "activo", True))
        if not activo:
            return
        empresa_id = self._empresa_del_servidor(attrs)
        if empresa_id is None:
            return
        gemelos = CentroCosto.objects.filter(empresa_id=empresa_id, codigo=codigo, activo=True)
        if self.instance is not None:
            gemelos = gemelos.exclude(pk=self.instance.pk)
        if gemelos.exists():
            raise ValidationError(
                {"codigo": "Ya existe un centro de costo con este código en la empresa."}
            )

    def _empresa_del_servidor(self, attrs):
        if self.instance is not None:
            return self.instance.empresa_id
        emp = attrs.get("empresa")
        if emp is not None:
            return getattr(emp, "pk", emp)
        user = getattr(self.context.get("request"), "user", None)
        return getattr(getattr(user, "empresa", None), "pk", None)


class PolizaDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PolizaDetalle
        fields = "__all__"
        extra_kwargs = {
            "poliza": {"read_only": True},
        }


class PolizaSerializer(CreateOnlyNestedLinesMixin, EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
    nested_write_on_create_only = ("poliza_detalles",)
    poliza_detalles = PolizaDetalleSerializer(many=True, required=False)
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
            # Misma resolución que FacturaProveedor/CuentaBancaria/CuentaPorPagar/
            # Cobro/Pago: ``empresa`` la fija el servidor y no llega en ``attrs``,
            # así que las comprobaciones de FK se apoyan en la empresa del usuario.
            # Colgarlas de ``emp`` las dejaba muertas y permitía guardar una
            # sucursal de otra empresa en un update.
            emp_id = getattr(emp, "pk", emp) if emp else getattr(user_empresa, "pk", None)
            if suc and emp_id and getattr(suc, "empresa_id", None) and suc.empresa_id != emp_id:
                raise ValidationError({"sucursal": "Sucursal no pertenece a la empresa."})
            if cc and emp_id and getattr(cc, "empresa_id", None) and cc.empresa_id != emp_id:
                raise ValidationError({"centro_costo": "Centro de costo no pertenece a la empresa."})
        return attrs


class FacturaProveedorDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = FacturaProveedorDetalle
        fields = "__all__"
        extra_kwargs = {
            "factura_proveedor": {"read_only": True},
        }


class FacturaProveedorSerializer(CreateOnlyNestedLinesMixin, EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
    nested_write_on_create_only = ("factura_proveedor_detalles",)
    factura_proveedor_detalles = FacturaProveedorDetalleSerializer(many=True, required=False)
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


class BancoSerializer(EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
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


class CuentaBancariaSerializer(EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
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


class CuentaPorPagarSerializer(EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
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
        factura = attrs.get("factura_proveedor")
        if factura is not None:
            self._validate_single_account_per_invoice(factura)
        if self.instance is None:
            if factura is not None:
                # Misma regla que la generación automática: sólo una factura
                # Registrada origina CxP. DRF convierte a 400 por campo el
                # ErrorDeNegocio lanzado desde validate().
                CuentaPorPagarService.ensure_invoice_registered(factura)
                self._validate_matches_invoice(attrs.get("proveedor"), attrs.get("total", 0), factura)
        elif self._changes_invoice_alignment(attrs):
            reapunta = factura is not None and factura.pk != self.instance.factura_proveedor_id
            if reapunta:
                # Re-apuntar la CxP sigue la misma regla que el alta: la factura
                # destino tiene que estar Registrada.
                CuentaPorPagarService.ensure_invoice_registered(factura)
            # En una edición se vuelve a cruzar contra la factura si cambia algo de
            # lo que ata la CxP a ella: si no, una CxP creada cuadrada podía
            # editarse después hacia otra factura, otro proveedor u otro total. Al
            # re-apuntarla se cruza además la moneda, que la CxP lee de su factura.
            self._validate_matches_invoice(
                attrs.get("proveedor", self.instance.proveedor),
                attrs.get("total", self.instance.total),
                factura if factura is not None else self.instance.factura_proveedor,
                moneda_id=self.instance.factura_proveedor.moneda_id if reapunta else None,
            )
        return attrs

    INVOICE_ALIGNED_FIELDS = ("factura_proveedor", "proveedor", "total")

    def _changes_invoice_alignment(self, attrs):
        # Sólo un valor distinto del vigente cuenta como cambio: un PUT que reenvía
        # los valores actuales no dispara el cruce.
        return any(
            field in attrs and attrs[field] != getattr(self.instance, field)
            for field in self.INVOICE_ALIGNED_FIELDS
        )

    def _validate_single_account_per_invoice(self, factura):
        # DRF no genera validador para un UniqueConstraint sobre un FK: sin esto
        # el duplicado llegaba a la base y volvía como IntegrityError (500).
        if self.instance is not None and self.instance.factura_proveedor_id == factura.pk:
            return
        if CuentaPorPagar.objects.filter(factura_proveedor=factura).exists():
            raise ValidationError({"factura_proveedor": DUPLICATE_INVOICE_MESSAGE})

    def _validate_matches_invoice(self, proveedor, total, factura, moneda_id=None):
        # La comparación vive en el servicio: es la misma que el ViewSet repite con
        # la factura ya bloqueada.
        CuentaPorPagarService.ensure_account_matches_invoice(
            factura, proveedor=proveedor, total=total, moneda_id=moneda_id,
        )


class CuentaPorPagarCreateSerializer(CuentaPorPagarSerializer):
    """Alta manual: ``saldo`` y ``estatus`` los fija el servidor (saldo = total,
    Pendiente), así que una CxP manual no puede nacer descuadrada.

    Es una clase aparte, y no un ``read_only`` condicionado a la instancia, para
    que el esquema OpenAPI refleje la ejecución: drf-spectacular construye los
    request de PUT/PATCH sin instancia y registra los componentes por nombre de
    serializer (el primero gana), así que con una sola clase ``saldo``/``estatus``
    salían de sólo lectura también en la edición, donde sí se aceptan.
    """

    class Meta(CuentaPorPagarSerializer.Meta):
        read_only_fields = ("saldo", "estatus")


class CobroDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = CobroDetalle
        fields = "__all__"
        extra_kwargs = {
            "cobro": {"read_only": True},
        }


class CobroSerializer(CreateOnlyNestedLinesMixin, EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
    nested_write_on_create_only = ("cobro_detalles",)
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
        extra_kwargs = {
            "pago": {"read_only": True},
        }


class PagoSerializer(CreateOnlyNestedLinesMixin, EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
    nested_write_on_create_only = ("pago_detalles",)
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
        extra_kwargs = {
            "nota_credito": {"read_only": True},
        }


class NotaCreditoSerializer(CreateOnlyNestedLinesMixin, serializers.ModelSerializer):
    nested_write_on_create_only = ("nota_credito_detalles",)
    nota_credito_detalles = NotaCreditoDetalleSerializer(many=True, required=False)
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

FEDERAL_TAXES = {"IVA", "ISR", "IEPS"}
# Catálogo c_ObjetoImp del SAT.
OBJETO_IMP_CHOICES = ["01", "02", "03", "04", "05"]
UNIT_CODE_RE = r"^[A-Za-z0-9]{2,3}$"          # c_ClaveUnidad (p. ej. E48, H87, KGM)
CODE_PROD_SERV_RE = r"^[0-9]{8}$"             # c_ClaveProdServ (8 dígitos)
CUENTA_PREDIAL_RE = r"^[0-9]{1,150}$"         # según la documentación
PEDIMENTO_RE = r"^[0-9]{2} [0-9]{2} [0-9]{4} [0-9]{7}$"  # "21 47 3807 8003832"
 

class ProductTaxSerializer(serializers.Serializer):
    Name = serializers.CharField(max_length=50)
    # Doc: [0-4][0-9]?.[0-9]{1,6}|0  ->  de 0 a 49.999999 con hasta 6 decimales
    Rate = serializers.DecimalField(
        max_digits=8,
        decimal_places=6,
        min_value=Decimal("0"),
        max_value=Decimal("49.999999"),
        required=False,
    )
    IsRetention = serializers.BooleanField(default=False)
    IsFederalTax = serializers.BooleanField(default=True)
    IsQuota = serializers.BooleanField(default=False)
    # Doc: [0-9]{1,18}(.[0-9]{1,6})?
    Total = serializers.DecimalField(
        max_digits=24,
        decimal_places=6,
        min_value=Decimal("0"),
        required=False,
    )
 
    def validate(self, attrs):
        if "Rate" not in attrs and "Total" not in attrs:
            raise serializers.ValidationError("Debes indicar 'Rate' o 'Total'.")
 
        if attrs.get("IsFederalTax", True):
            name = attrs.get("Name", "").upper()
            if name and name not in FEDERAL_TAXES:
                raise serializers.ValidationError(
                    {"Name": f"Un impuesto federal debe ser uno de: {', '.join(sorted(FEDERAL_TAXES))}."}
                )
            attrs["Name"] = name
        return attrs
 
class ItemComplementSerializer(serializers.Serializer):
    EducationalInstitution = serializers.DictField(required=False)
    ThirdPartyAccount = serializers.DictField(required=False)
    HidroYPetro = serializers.DictField(required=False)

class FacturamaProductSerializer(serializers.Serializer):
    Unit = serializers.CharField(min_length=1, max_length=20)
    UnitCode = serializers.RegexField(
        UNIT_CODE_RE,
        error_messages={"invalid": "Clave de unidad SAT inválida (2 o 3 caracteres alfanuméricos, p. ej. E48, H87)."},
    )
    IdentificationNumber = serializers.CharField(
        max_length=50, required=False, allow_blank=True, allow_null=True
    )
    Name = serializers.CharField(min_length=2, max_length=50)
    Description = serializers.CharField(max_length=1000)
    Price = serializers.DecimalField(
        max_digits=24, decimal_places=6, min_value=Decimal("0")
    )
    CodeProdServ = serializers.RegexField(
        CODE_PROD_SERV_RE,
        error_messages={"invalid": "La clave de producto/servicio del SAT debe tener 8 dígitos."},
    )
    CodeProdServName = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    CuentaPredial = serializers.RegexField(
        CUENTA_PREDIAL_RE,
        required=False,
        allow_null=True,
        error_messages={"invalid": "La cuenta predial solo admite dígitos (1 a 150)."},
    )
    CuentasPredial = serializers.ListField(
        child=serializers.RegexField(
            CUENTA_PREDIAL_RE,
            error_messages={"invalid": "La cuenta predial solo admite dígitos (1 a 150)."},
        ),
        required=False,
        allow_null=True,
    )
    NumerosPedimento = serializers.ListField(
        child=serializers.RegexField(
            PEDIMENTO_RE,
            error_messages={"invalid": "Formato de pedimento inválido. Usa 'AA AA NNNN NNNNNNN'."},
        ),
        required=False,
        allow_null=True,
    )
    Complement = ItemComplementSerializer(required=False, allow_null=True)
    Taxes = ProductTaxSerializer(many=True, required=False, allow_null=True)
    ObjetoImp = serializers.ChoiceField(
        choices=OBJETO_IMP_CHOICES, required=False, allow_null=True
    )
 
 
    def validate_UnitCode(self, value):
        return value.upper()
 
    def validate_Taxes(self, taxes):
        seen = set()
        for tax in taxes or []:
            key = (
                tax["Name"].upper(),
                tax.get("IsRetention", False),
                tax.get("IsFederalTax", True),
            )
            if key in seen:
                kind = "retención" if key[1] else "traslado"
                raise serializers.ValidationError(
                    f"El impuesto '{key[0]}' ({kind}) está repetido."
                )
            seen.add(key)
        return taxes
 
    def validate(self, attrs):
        objeto_imp = attrs.get("ObjetoImp")
        taxes = attrs.get("Taxes")
 
        if objeto_imp == "01" and taxes:
            raise serializers.ValidationError(
                {"Taxes": "Con ObjetoImp '01' (no objeto de impuesto) no se deben enviar impuestos."}
            )
        if objeto_imp == "02" and not self.partial and not taxes:
            raise serializers.ValidationError(
                {"Taxes": "Con ObjetoImp '02' (sí objeto de impuesto) se requiere al menos un impuesto."}
            )
        return attrs
 
 
    def to_facturama_payload(self) -> dict:
        return _clean(self.validated_data)
 
def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    return value

class FacturamaReceiverSerializer(serializers.Serializer):
    Rfc = serializers.CharField(
        max_length=13,
        min_length=12
    )
    Name = serializers.CharField(
        max_length=254
    )
    CfdiUse = serializers.CharField()
    FiscalRegime = serializers.CharField()
    TaxZipCode = serializers.CharField(
        max_length=5,
        min_length=5
    )

class FacturamaItemSerializer(serializers.Serializer):
    ProductCode = serializers.CharField()
    Description = serializers.CharField(
        max_length=1000
    )
    UnitCode = serializers.CharField()
    Quantity = serializers.DecimalField(
        max_digits=18,
        decimal_places=6
    )
    UnitPrice = serializers.DecimalField(
        max_digits=18,
        decimal_places=6
    )
    TaxObject = serializers.CharField()

class FacturamaCfdiCreateSerializer(serializers.Serializer):
    NameId = serializers.IntegerField(
        required=False,
        default=1
    )

    Date = serializers.DateTimeField()

    Currency = serializers.CharField(
        max_length=3,
        min_length=3
    )

    ExpeditionPlace = serializers.CharField(
        max_length=5,
        min_length=5
    )

    Exportation = serializers.ChoiceField(
        choices=[
            ("01", "No aplica"),
            ("02", "Definitiva con clave A1"),
            ("03", "Temporal"),
            ("04", "Definitiva distinta de A1"),
        ]
    )

    CfdiType = serializers.ChoiceField(
        choices=[
            ("I", "Ingreso"),
            ("E", "Egreso"),
            ("T", "Traslado"),
            ("N", "Nómina"),
            ("P", "Pago"),
        ]
    )

    PaymentForm = serializers.ChoiceField(
        choices=[
            ("01", "Efectivo"),
            ("02", "Cheque nominativo"),
            ("03", "Transferencia electrónica"),
            ("04", "Tarjeta de crédito"),
            ("05", "Monedero electrónico"),
            ("06", "Dinero electrónico"),
            ("08", "Vales de despensa"),
            ("12", "Dación en pago"),
            ("13", "Pago por subrogación"),
            ("14", "Pago por consignación"),
            ("15", "Condonación"),
            ("17", "Compensación"),
            ("23", "Novación"),
            ("24", "Confusión"),
            ("25", "Remisión de deuda"),
            ("26", "Prescripción o caducidad"),
            ("27", "A satisfacción del acreedor"),
            ("28", "Tarjeta de débito"),
            ("29", "Tarjeta de servicios"),
            ("30", "Aplicación de anticipos"),
            ("31", "Intermediario pagos"),
            ("99", "Por definir"),
        ],
        required=False
    )

    PaymentMethod = serializers.ChoiceField(
        choices=[
            ("PUE", "Pago en una sola exhibición"),
            ("PPD", "Pago en parcialidades o diferido"),
        ],
        required=False
    )

    Receiver = FacturamaReceiverSerializer()

    Items = FacturamaItemSerializer(
        many=True,
        allow_empty=False
    )

    def validate(self, attrs):
        payment_method = attrs.get("PaymentMethod")
        payment_form = attrs.get("PaymentForm")

        if payment_method == "PPD" and payment_form != "99":
            raise serializers.ValidationError({
                "PaymentForm": (
                    "PaymentForm debe ser '99' cuando "
                    "PaymentMethod es 'PPD'."
                )
            })

        return attrs

class FacturamaCfdiFileSerializer(serializers.Serializer):

    FORMAT_CHOICES = [
        ("pdf", "PDF"),
        ("html", "HTML"),
        ("xml", "XML"),
    ]

    TYPE_CHOICES = [
        ("payroll", "Payroll"),
        ("received", "Received"),
        ("issued", "Issued"),
        ("issuedLite", "Issued Lite"),
    ]

    format = serializers.ChoiceField(
        choices=FORMAT_CHOICES
    )

    type = serializers.ChoiceField(
        choices=TYPE_CHOICES
    )

    id = serializers.CharField(
        required=True
    )

class FacturamaAcuseSerializer(serializers.Serializer):

    FORMAT_CHOICES = [
        ("pdf", "PDF"),
        ("html", "HTML"),
    ]

    TYPE_CHOICES = [
        ("payroll", "Payroll"),
        ("issued", "Issued"),
        ("issuedLite", "Issued Lite"),
    ]

    format = serializers.ChoiceField(
        choices=FORMAT_CHOICES
    )

    type = serializers.ChoiceField(
        choices=TYPE_CHOICES
    )

    id = serializers.CharField(
        required=True
    )