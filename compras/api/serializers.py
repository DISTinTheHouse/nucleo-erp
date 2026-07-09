from rest_framework import serializers
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle


class OrdenCompraDetalleReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenCompraDetalle
        fields = ['producto_id', 'descripcion', 'cantidad', 'descuento', 'importe', 'piezas', 'precio']


class OrdenCompraSerializer(serializers.ModelSerializer):
    estatus_label = serializers.SerializerMethodField()
    proveedor_nombre = serializers.SerializerMethodField()
    proveedor_correo = serializers.SerializerMethodField()

    class Meta:
        model = OrdenCompra
        fields = [
            'id',
            'empresa',
            'sucursal',
            'proveedor',
            'proveedor_nombre',
            'proveedor_correo',
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

    def get_proveedor_correo(self, obj):
        return obj.proveedor.email if obj.proveedor_id else None

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


class RecepcionDetalleResumenSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source="producto.nombre", read_only=True)
    ubicacion_nombre = serializers.CharField(
        source="ubicacion.nombre", read_only=True, default=None
    )

    class Meta:
        model = RecepcionDetalle
        fields = [
            "id",
            "orden_compra_detalle",
            "producto",
            "producto_nombre",
            "producto_variante",
            "ubicacion",
            "ubicacion_nombre",
            "lote",
            "serie",
            "cantidad_recibida",
        ]


class RecepcionRelacionadaSerializer(serializers.ModelSerializer):
    estatus_label = serializers.CharField(source="get_estatus_display", read_only=True)
    sucursal_nombre = serializers.CharField(source="sucursal.nombre", read_only=True)
    proveedor_nombre = serializers.CharField(
        source="proveedor.nombre", read_only=True, default=None
    )
    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True)
    transportista_nombre = serializers.CharField(
        source="transportista.nombre", read_only=True, default=None
    )
    detalles = RecepcionDetalleResumenSerializer(
        many=True,
        read_only=True,
        source="recepciondetalle_set",
    )

    class Meta:
        model = Recepcion
        fields = [
            "id",
            "tipo_origen",
            "folio",
            "remision",
            "factura_referencia",
            "fecha_recepcion",
            "estatus",
            "estatus_label",
            "sucursal",
            "sucursal_nombre",
            "proveedor",
            "proveedor_nombre",
            "almacen",
            "almacen_nombre",
            "transportista",
            "transportista_nombre",
            "observaciones",
            "created_at",
            "updated_at",
            "detalles",
        ]


class OrdenCompraRetrieveSerializer(OrdenCompraSerializer):
    detalles = OrdenCompraDetalleReadSerializer(many=True, read_only=True, source='ordencompradetalle_set')
    recepciones = RecepcionRelacionadaSerializer(
        many=True,
        read_only=True,
        source="recepcion_set",
    )

    class Meta(OrdenCompraSerializer.Meta):
        fields = OrdenCompraSerializer.Meta.fields + ['detalles', 'recepciones']


class OrdenCompraOnboardingHeaderSerializer(serializers.Serializer):
    sucursal = serializers.IntegerField(required=False, allow_null=True)
    proveedor = serializers.IntegerField(required=False, allow_null=True)
    moneda = serializers.IntegerField(required=False, allow_null=True)
    fecha_oc = serializers.DateField(required=False, allow_null=True)
    porcentaje_iva = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
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
            "orden_produccion_detalle",
            "producto",
            "producto_variante",
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


class RecepcionListSerializer(RecepcionSerializer):
    """Listado de recepciones para el módulo de Compras.

    Conserva intacta la forma plana de ``RecepcionSerializer`` (``fields='__all__'``,
    consumida hoy por WMS) y solo AÑADE campos legibles resueltos a través de las FK
    que el queryset del viewset ya trae con ``select_related`` — sin consultas extra:

      - ``almacen_nombre``: siempre presente (``almacen`` es NOT NULL).
      - ``proveedor_nombre``: ``null`` cuando la recepción no tiene proveedor (p. ej. OP).
      - ``orden_compra_folio``: ``null`` salvo que la recepción provenga de una
        orden de compra (``tipo_origen == 'OC'``).

    El enriquecimiento del lado OP (folio/referencia de la orden de producción) queda
    fuera de este paso; puede sumarse aquí después con el mismo patrón (``op_folio``…).
    """

    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True)
    proveedor_nombre = serializers.CharField(
        source="proveedor.nombre", read_only=True, default=None
    )
    orden_compra_folio = serializers.CharField(
        source="orden_compra.folio", read_only=True, default=None
    )

    class Meta(RecepcionSerializer.Meta):
        pass


class RecepcionDetalleRetrieveSerializer(serializers.ModelSerializer):
    """Renglón anidado del ``retrieve`` individual de una recepción.

    Resuelve ``producto_nombre`` y ``ubicacion_nombre`` a través de las FK que el
    ``prefetch_related`` del viewset trae con ``select_related`` — sin consultas
    por fila (a diferencia de ``RecepcionDetalleResumenSerializer``, cuyo
    ``recepciondetalle_set`` no venía optimizado).

    Sirve tanto a renglones de OC como de OP: ``producto`` es NOT NULL en el
    modelo (el onboarding lo puebla en ambos orígenes), así que
    ``producto_nombre`` siempre resuelve. En un renglón de OC ``producto_variante``
    y ``orden_compra_detalle`` conviven con ``orden_produccion_detalle`` nulo; en
    uno de OP es al revés (``producto_variante`` poblado, ``orden_compra_detalle``
    nulo). Esta forma refleja el shape objetivo, que expone ``orden_compra_detalle``
    pero no ``orden_produccion_detalle``.
    """

    producto_nombre = serializers.CharField(source="producto.nombre", read_only=True)
    ubicacion_nombre = serializers.CharField(
        source="ubicacion.nombre", read_only=True, default=None
    )

    class Meta:
        model = RecepcionDetalle
        fields = [
            "id",
            "orden_compra_detalle",
            "producto",
            "producto_nombre",
            "producto_variante",
            "ubicacion",
            "ubicacion_nombre",
            "lote",
            "serie",
            "cantidad_recibida",
        ]


class RecepcionRetrieveSerializer(serializers.ModelSerializer):
    """Detalle individual de una recepción (acción ``retrieve``).

    Forma dedicada, independiente de ``RecepcionListSerializer`` (que conserva la
    forma plana ``__all__`` que consume WMS/list): expone las FK resueltas
    (``estatus_label``, ``sucursal_nombre``…) y anida los renglones en
    ``detalles``. Deliberadamente NO incluye ``orden_compra``/``op``/
    ``orden_compra_folio``/``activo`` — no forman parte del shape objetivo del
    detalle. Se deja separada del chain de retrieve de OrdenCompra
    (``RecepcionRelacionadaSerializer``) para no acoplar ambos caminos.
    """

    estatus_label = serializers.CharField(source="get_estatus_display", read_only=True)
    sucursal_nombre = serializers.CharField(source="sucursal.nombre", read_only=True)
    proveedor_nombre = serializers.CharField(
        source="proveedor.nombre", read_only=True, default=None
    )
    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True)
    transportista_nombre = serializers.CharField(
        source="transportista.nombre", read_only=True, default=None
    )
    detalles = RecepcionDetalleRetrieveSerializer(
        many=True,
        read_only=True,
        source="recepciondetalle_set",
    )

    class Meta:
        model = Recepcion
        fields = [
            "id",
            "tipo_origen",
            "folio",
            "remision",
            "factura_referencia",
            "fecha_recepcion",
            "estatus",
            "estatus_label",
            "sucursal",
            "sucursal_nombre",
            "proveedor",
            "proveedor_nombre",
            "almacen",
            "almacen_nombre",
            "transportista",
            "transportista_nombre",
            "observaciones",
            "created_at",
            "updated_at",
            "detalles",
        ]


class RecepcionOnboardingHeaderSerializer(serializers.Serializer):
    orden_compra = serializers.IntegerField(required=False, allow_null=True)
    orden_produccion = serializers.IntegerField(required=False, allow_null=True)
    almacen = serializers.IntegerField()
    serie_codigo = serializers.CharField(required=False, allow_blank=True)
    fecha_recepcion = serializers.DateTimeField(required=False, allow_null=True)
    remision = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    factura_referencia = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    observaciones = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    transportista = serializers.IntegerField(required=False, allow_null=True)


class RecepcionOnboardingDetalleInputSerializer(serializers.Serializer):
    orden_compra_detalle = serializers.IntegerField(required=False, allow_null=True)
    orden_produccion_detalle = serializers.IntegerField(required=False, allow_null=True)
    cantidad_recibida = serializers.DecimalField(max_digits=18, decimal_places=4)
    ubicacion = serializers.IntegerField(required=False, allow_null=True)
    lote = serializers.IntegerField(required=False, allow_null=True)
    serie = serializers.IntegerField(required=False, allow_null=True)


class RecepcionOnboardingSerializer(serializers.Serializer):
    recepcion = RecepcionOnboardingHeaderSerializer()
    detalle = RecepcionOnboardingDetalleInputSerializer(many=True)

