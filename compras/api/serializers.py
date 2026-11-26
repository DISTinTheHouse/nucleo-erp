from decimal import Decimal

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field

from rest_framework import serializers
from nucleo.listados import nombre_para_mostrar_usuario as _nombre_usuario
from compras.models import (
    CalidadInspeccion,
    CalidadInspeccionDetalle,
    OrdenCompra,
    OrdenCompraDetalle,
    OrdenCompraRutaCritica,
    Recepcion,
    RecepcionDetalle,
    RecepcionRFIDEncuadre,
    RecepcionRFIDLectura,
)


class OrdenCompraDetalleReadSerializer(serializers.ModelSerializer):
    # ``id`` permite casar cada renglón de la OC con el ``orden_compra_detalle``
    # que referencian los renglones anidados en ``recepciones[].detalles[]``.
    # ``producto`` es NOT NULL en el modelo, así que ``source=`` basta (mismo
    # patrón que ``RecepcionDetalleResumenSerializer.producto_nombre``).
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    # Identifica la materia prima, que no tiene variante (``producto`` va en el select_related).
    producto_codigo = serializers.CharField(source='producto.codigo', read_only=True, default=None)
    producto_cod_proscai = serializers.CharField(source='producto.cod_proscai', read_only=True, default=None)
    producto_variante_sku = serializers.CharField(
        source='producto_variante.sku', read_only=True, default=None
    )
    # Mismos nombres que el onboarding de recepciones; ``None`` en renglones
    # de MP o legados sin variante.
    producto_variante_color = serializers.CharField(
        source='producto_variante.color.nombre', read_only=True, default=None
    )
    producto_variante_talla = serializers.CharField(
        source='producto_variante.talla.nombre', read_only=True, default=None
    )

    class Meta:
        model = OrdenCompraDetalle
        fields = [
            'id',
            'producto_id',
            'producto_nombre',
            'producto_codigo',
            'producto_cod_proscai',
            'producto_variante_id',
            'producto_variante_sku',
            'producto_variante_color',
            'producto_variante_talla',
            'descripcion',
            'cantidad',
            'descuento',
            'importe',
            'piezas',
            'precio',
        ]


def direccion_sucursal(sucursal):
    """Dirección de la sucursal en una línea (``None`` si no tiene ninguna)."""
    if sucursal is None:
        return None
    partes = [
        sucursal.direccion_linea1, sucursal.direccion_linea2, sucursal.ciudad, sucursal.estado,
        f"C.P. {sucursal.cp}" if sucursal.cp else None, sucursal.pais,
    ]
    return ", ".join(p.strip() for p in partes if p and p.strip()) or None


def con_pre_ordenes(qs):
    """Anota ``pre_ordenes_ids`` para ``OrdenCompraSerializer.get_pre_ordenes``.

    Sin la anotación, el serializer consulta las necesidades de cada OC (N+1, #1025).
    Una sola definición para el listado, el historial por proveedor y las respuestas
    de escritura.
    """
    from django.contrib.postgres.expressions import ArraySubquery
    from django.db.models import OuterRef
    from compras.models import NecesidadCompra

    return qs.annotate(pre_ordenes_ids=ArraySubquery(
        NecesidadCompra.objects.filter(orden_compra_id=OuterRef("pk")).order_by()
        .values("pre_orden_id").distinct()
    ))


class OrdenCompraSerializer(serializers.ModelSerializer):
    def get_pre_ordenes(self, obj):
        ids = getattr(obj, "pre_ordenes_ids", None)
        if ids is None:
            ids = obj.necesidades.values_list("pre_orden_id", flat=True).distinct()
        ids = set(ids or [])
        if obj.pre_orden_id:
            ids.add(obj.pre_orden_id)
        return sorted(ids)

    estatus_label = serializers.SerializerMethodField()
    proveedor_nombre = serializers.SerializerMethodField()
    proveedor_correo = serializers.SerializerMethodField()
    # Etiquetas legibles junto a las FKs, mismo patrón ``source=`` que
    # ``RecepcionRelacionadaSerializer`` y que los serializers de producción.
    # ``Empresa`` no tiene ``nombre``: su nombre humano es ``razon_social``.
    # ``Moneda.simbolo`` y ``Pedido.folio`` son nullable, y ``pedido`` es además
    # una FK opcional (``SET_NULL``): sin ``allow_null=True`` DRF omitiría el
    # campo (``SkipField``) en vez de devolver ``null``.
    empresa_nombre = serializers.CharField(source='empresa.razon_social', read_only=True)
    # Pre-orden de producción que generó la OC (faltantes), o ``null``.
    pre_orden_id = serializers.IntegerField(read_only=True, allow_null=True)
    # Pre-órdenes de la OC: la del flujo anterior (``pre_orden_id``) más las de
    # sus necesidades consolidadas (anotado en el queryset: ``pre_ordenes_ids``).
    pre_ordenes = serializers.SerializerMethodField()
    sucursal_nombre = serializers.CharField(source='sucursal.nombre', read_only=True)
    # Dirección de entrega para el proveedor: la de la sucursal de la OC.
    sucursal_direccion = serializers.SerializerMethodField()
    sucursal_telefono = serializers.CharField(source='sucursal.telefono', read_only=True, default=None)
    almacen_nombre = serializers.CharField(source='almacen.nombre', read_only=True, default=None)
    usuario_nombre = serializers.SerializerMethodField()
    # ``cerrada_por`` va en el ``select_related`` de cada queryset (#1373).
    cerrada_por_nombre = serializers.SerializerMethodField()
    moneda_codigo = serializers.CharField(source='moneda.codigo_iso', read_only=True)
    moneda_simbolo = serializers.CharField(source='moneda.simbolo', read_only=True, allow_null=True)
    pedido_folio = serializers.CharField(source='pedido.folio', read_only=True, allow_null=True)
    tipo_compra_label = serializers.CharField(source='get_tipo_compra_display', read_only=True)
    pedidos = serializers.SerializerMethodField()
    pedidos_vinculados = serializers.SerializerMethodField()
    pedido_cancelado_origen_folio = serializers.SerializerMethodField()
    # Progreso de recepción contra ``total_piezas``: ``progreso_recibido`` lo
    # anota ``anotar_progreso_oc`` en list/retrieve; en las demás respuestas sale ``null``.
    piezas_recibidas = serializers.SerializerMethodField()
    piezas_pendientes = serializers.SerializerMethodField()
    progreso_pct = serializers.SerializerMethodField()

    def _recibidas(self, obj):
        valor = getattr(obj, "progreso_recibido", None)
        return None if valor is None else max(valor, Decimal("0"))

    def get_piezas_recibidas(self, obj):
        recibidas = self._recibidas(obj)
        return None if recibidas is None else str(recibidas.quantize(Decimal("0.0001")))

    def get_piezas_pendientes(self, obj):
        recibidas = self._recibidas(obj)
        if recibidas is None:
            return None
        return str(max(Decimal(obj.total_piezas or 0) - recibidas, Decimal("0")).quantize(Decimal("0.0001")))

    def get_progreso_pct(self, obj):
        recibidas = self._recibidas(obj)
        if recibidas is None:
            return None
        total = Decimal(obj.total_piezas or 0)
        if total <= 0:
            return 0.0
        return float(round(min(recibidas / total * 100, Decimal("100")), 1))

    def _pedidos_de(self, obj):
        # ``pedidos.all()`` aprovecha el prefetch del viewset. Si la OC solo
        # trae el FK legado ``pedido`` (alta desde el admin), cuenta como uno.
        pedidos = list(obj.pedidos.all())
        if not pedidos and obj.pedido_id:
            pedidos = [obj.pedido]
        # La M2M no guarda posición: el orden es estable (el pedido principal
        # primero, el resto por pk) para que ``pedidos[0]`` coincida con ``pedido``.
        pedidos.sort(key=lambda p: (p.pk != obj.pedido_id, p.pk))
        return pedidos

    def get_pedidos(self, obj):
        return [p.pk for p in self._pedidos_de(obj)]

    def get_pedido_cancelado_origen_folio(self, obj) -> str | None:
        """Folio del pedido que puso la marca ``pedido_cancelado`` (#1404). Es uno de los
        pedidos de la OC, ya cargados para ``pedidos_vinculados``: sin consultas."""
        if not obj.pedido_cancelado_origen_id:
            return None
        return next((p.folio for p in self._pedidos_de(obj) if p.pk == obj.pedido_cancelado_origen_id), None)

    def get_pedidos_vinculados(self, obj):
        # ``estatus``/``activo``/``cliente_nombre`` (EC-782): la OC avisa si un pedido
        # ligado se canceló o se dio de baja sin consultar cada pedido aparte. Son
        # columnas de la fila que ya trae el prefetch de ``pedidos`` (o el
        # ``select_related`` del FK legado): ninguna consulta más.
        return [
            {'id': p.pk, 'folio': p.folio, 'estatus': p.estatus, 'activo': p.activo, 'cliente_nombre': p.cliente_nombre}
            for p in self._pedidos_de(obj)
        ]

    class Meta:
        model = OrdenCompra
        fields = [
            'id',
            'empresa',
            'empresa_nombre',
            'sucursal',
            'sucursal_nombre',
            'sucursal_direccion',
            'sucursal_telefono',
            'almacen',
            'almacen_nombre',
            'proveedor',
            'proveedor_nombre',
            'proveedor_correo',
            'solicitud_compra',
            'moneda',
            'moneda_codigo',
            'moneda_simbolo',
            'tipo_cambio',
            'usuario',
            'usuario_nombre',
            'pedido',
            'pedido_folio',
            'pre_orden_id',
            'pre_ordenes',
            'pedidos',
            'pedidos_vinculados',
            'tipo_compra',
            'tipo_compra_label',
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
            'piezas_recibidas',
            'piezas_pendientes',
            'progreso_pct',
            'flete',
            'seguros',
            'porcentaje_iva',
            'total_iva',
            'gran_total',
            'a_cuenta',
            'observaciones',
            'motivo_cancelacion',
            'saldo_cerrado',
            'motivo_cierre',
            'fecha_cierre',
            'cerrada_por',
            'cerrada_por_nombre',
            'pedido_cancelado',
            'fecha_pedido_cancelado',
            'pedido_cancelado_origen',
            'pedido_cancelado_origen_folio',
            'activo',
            'created_at',
            'updated_at',
        ]
        # Solo los escriben ``cancelar`` y ``cerrar-saldo``.
        read_only_fields = ['motivo_cancelacion', 'pedido_cancelado', 'fecha_pedido_cancelado', 'pedido_cancelado_origen', 'saldo_cerrado', 'motivo_cierre', 'fecha_cierre', 'cerrada_por']
        extra_kwargs = {
            'folio': {'required': False, 'allow_null': True},
            'solicitud_compra': {'required': False, 'allow_null': True},
            'pedido': {'required': False, 'allow_null': True},
            'proveedor': {'required': False, 'allow_null': True},
        }

    def get_estatus_label(self, obj):
        return obj.get_estatus_display()

    def get_sucursal_direccion(self, obj):
        return direccion_sucursal(obj.sucursal)

    def get_proveedor_nombre(self, obj):
        return obj.proveedor.nombre if obj.proveedor_id else None

    def get_proveedor_correo(self, obj):
        return obj.proveedor.email if obj.proveedor_id else None

    def get_usuario_nombre(self, obj):
        return _nombre_usuario(obj.usuario)

    def get_cerrada_por_nombre(self, obj):
        return _nombre_usuario(obj.cerrada_por) if obj.cerrada_por_id else None

    def to_internal_value(self, data):
        # Convert "0" or 0 to None for FK fields if they are optional
        for field in ['solicitud_compra', 'pedido', 'proveedor']:
            if field in data and (data[field] == "0" or data[field] == 0):
                data[field] = None
        return super().to_internal_value(data)


class OrdenCompraDetalleSerializer(serializers.ModelSerializer):
    producto_variante_sku = serializers.CharField(
        source='producto_variante.sku', read_only=True, default=None
    )
    producto_variante_color = serializers.CharField(
        source='producto_variante.color.nombre', read_only=True, default=None
    )
    producto_variante_talla = serializers.CharField(
        source='producto_variante.talla.nombre', read_only=True, default=None
    )

    class Meta:
        model = OrdenCompraDetalle
        fields = "__all__"


class RecepcionDetalleResumenSerializer(serializers.ModelSerializer):
    """Renglón de recepción anidado: en ``recepciones[].detalles[]`` del retrieve
    de la OC y, vía ``RecepcionDetalleRetrieveSerializer``, en ``detalles[]`` del
    retrieve de la recepción.

    Las FK que resuelve (producto, variante y su talla, ubicación y su almacén)
    llegan en el ``select_related`` del ``Prefetch`` de ``recepciondetalle_set``
    de ambos viewsets: sin consultas por fila. Mantén los dos ``Prefetch`` al
    día si agregas un campo que cruce otra FK.

    Sirve tanto a renglones de OC como de OP. Un renglón de muestra (OP de
    fabricación especial) no trae ``producto``: ``producto_nombre``, el SKU y la
    talla salen de ``variante_produccion`` (también en el ``select_related``).
    En un renglón de OC ``producto_variante``
    y ``orden_compra_detalle`` conviven con ``orden_produccion_detalle`` nulo; en
    uno de OP es al revés (``producto_variante`` poblado, ``orden_compra_detalle``
    nulo). La forma expone ``orden_compra_detalle`` pero no
    ``orden_produccion_detalle``.
    """

    producto_nombre = serializers.CharField(source="articulo_nombre", read_only=True, default=None)
    # Identifica la materia prima, que no tiene variante (``producto`` va en el select_related).
    producto_codigo = serializers.CharField(source="producto.codigo", read_only=True, default=None)
    producto_cod_proscai = serializers.CharField(source="producto.cod_proscai", read_only=True, default=None)
    # ``producto_variante`` viene en el ``select_related`` de ambos retrieves.
    producto_variante_sku = serializers.CharField(source="articulo_sku", read_only=True, default=None)
    # Mismo nombre que en los renglones de la OC; ``None`` sin variante o sin
    # talla. ``producto_variante__talla`` va en el mismo ``select_related``.
    producto_variante_talla = serializers.CharField(source="articulo_talla", read_only=True, default=None)
    ubicacion_nombre = serializers.SerializerMethodField()
    # #1122: código legible junto al id (``lote``/``serie`` van en el select_related).
    # ``numero_serie`` y no ``serie_codigo``: ese nombre ya es la serie de folios
    # en el encabezado de la recepción.
    lote_codigo = serializers.CharField(source="lote.codigo", read_only=True, default=None)
    numero_serie = serializers.CharField(source="serie.codigo", read_only=True, default=None)

    class Meta:
        model = RecepcionDetalle
        fields = [
            "id",
            "orden_compra_detalle",
            "producto",
            "producto_nombre",
            "producto_codigo",
            "producto_cod_proscai",
            "producto_variante",
            "producto_variante_sku",
            "producto_variante_talla",
            "variante_produccion",
            "salida_maquila_detalle",
            "ubicacion",
            "ubicacion_nombre",
            "lote",
            "lote_codigo",
            "serie",
            "numero_serie",
            "cantidad_recibida",
        ]

    def get_ubicacion_nombre(self, obj):
        return str(obj.ubicacion) if obj.ubicacion_id else None


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
    pedido_vinculado = serializers.SerializerMethodField()
    documentos = serializers.SerializerMethodField()

    def get_pedido_vinculado(self, obj):
        from compras.services.orden_compra_view_service import pedido_vinculado_de_oc
        return pedido_vinculado_de_oc(obj)

    def get_documentos(self, obj):
        from compras.services.orden_compra_view_service import listar_documentos_orden_compra
        return listar_documentos_orden_compra(obj)

    class Meta(OrdenCompraSerializer.Meta):
        fields = OrdenCompraSerializer.Meta.fields + ['detalles', 'recepciones', 'pedido_vinculado', 'documentos']


class OrdenCompraOnboardingHeaderSerializer(serializers.Serializer):
    sucursal = serializers.IntegerField(required=False, allow_null=True)
    # Almacén destino de la recepción; ``null`` lo quita.
    almacen = serializers.IntegerField(required=False, allow_null=True)
    proveedor = serializers.IntegerField(required=False, allow_null=True)
    moneda = serializers.IntegerField(required=False, allow_null=True)
    # Pesos por unidad de ``moneda``: obligatorio si no es MXN (#809).
    tipo_cambio = serializers.DecimalField(
        max_digits=12, decimal_places=6, required=False, allow_null=True, min_value=Decimal("0.000001"),
    )
    # ``fecha_oc`` (fecha de generación) ya NO se acepta aquí: la fija el
    # servidor al crear la OC (ver EC-395) y no es editable. Se deja de
    # declarar a propósito para que un valor enviado por el cliente se
    # ignore en vez de validarse como si fuera a aplicarse.
    fecha_vencimiento = serializers.DateField(required=False, allow_null=True)
    porcentaje_iva = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    referencia = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=50)
    observaciones = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    # ``pedidos`` (lista) manda sobre ``pedido`` (uno, legado). ``[]``/``null``
    # desvincula. Solo se tocan si vienen en el body.
    pedido = serializers.IntegerField(required=False, allow_null=True)
    pedidos = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True, allow_empty=True,
    )
    tipo_compra = serializers.ChoiceField(choices=OrdenCompra.TipoCompra.choices, required=False)


class OrdenCompraOnboardingDetalleInputSerializer(serializers.Serializer):
    # Uno de los dos: con ``producto_variante`` (OC de PT, por SKU) el producto
    # sale de la variante.
    producto = serializers.IntegerField(required=False, allow_null=True)
    producto_variante = serializers.IntegerField(required=False, allow_null=True)
    cantidad = serializers.IntegerField(min_value=1)
    precio = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    descripcion = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def validate(self, attrs):
        if not attrs.get("producto") and not attrs.get("producto_variante"):
            raise serializers.ValidationError({"producto": "Envía `producto` o `producto_variante`."})
        return attrs


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


class _CancelacionRecepcionMixin(serializers.Serializer):
    """Quién canceló la recepción (EC-926), como ``usuario``/``usuario_nombre``.
    ``cancelado_por`` va en el ``select_related`` del viewset."""

    cancelado_por_nombre = serializers.SerializerMethodField()

    def get_cancelado_por_nombre(self, obj):
        return _nombre_usuario(obj.cancelado_por) if obj.cancelado_por_id else None


class RecepcionSerializer(_CancelacionRecepcionMixin, serializers.ModelSerializer):
    class Meta:
        model = Recepcion
        fields = '__all__'
        read_only_fields = ('motivo_cancelacion', 'fecha_cancelacion', 'cancelado_por')


class _OrigenSalidaMaquilaMixin(serializers.Serializer):
    """SM de la recepción y su documento de origen (#1237): ``SM-00005 · OB-00012``.
    ``salida_maquila`` y sus ``ob``/``orden_r``/``op`` van en el ``select_related``
    del viewset: sin consultas extra. Todo ``null`` si no es recepción de SM."""

    salida_maquila_folio = serializers.CharField(source="salida_maquila.folio", read_only=True, default=None)
    salida_maquila_origen = serializers.SerializerMethodField()
    salida_maquila_origen_folio = serializers.SerializerMethodField()

    def _origen(self, obj):
        sm = obj.salida_maquila if obj.salida_maquila_id else None
        if sm is None:
            return None, None
        if sm.ob_id:
            return "OB", sm.ob.folio_bordado
        if sm.orden_r_id:
            return "OR", sm.orden_r.folio_reflejante
        return "OP", sm.op.folio_op

    def get_salida_maquila_origen(self, obj):
        return self._origen(obj)[0]

    def get_salida_maquila_origen_folio(self, obj):
        return self._origen(obj)[1]


class RecepcionListSerializer(_OrigenSalidaMaquilaMixin, RecepcionSerializer):
    """Listado de recepciones para el módulo de Compras.

    Conserva intacta la forma plana de ``RecepcionSerializer`` (``fields='__all__'``,
    consumida hoy por WMS) y solo AÑADE campos legibles resueltos a través de las FK
    que el queryset del viewset ya trae con ``select_related`` — sin consultas extra:

      - ``almacen_nombre``: siempre presente (``almacen`` es NOT NULL).
      - ``proveedor_nombre``: ``null`` cuando la recepción no tiene proveedor (p. ej. OP).
      - ``orden_compra_folio``: ``null`` salvo que la recepción provenga de una
        orden de compra (``tipo_origen == 'OC'``); también ``null`` si la OC no
        tiene folio.
      - ``op_folio``: ``null`` salvo que la recepción provenga de una orden de
        producción (``tipo_origen == 'OP'``). Misma declaración que el retrieve
        (EC-879).
    """

    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True)
    proveedor_nombre = serializers.CharField(
        source="proveedor.nombre", read_only=True, default=None
    )
    orden_compra_folio = serializers.CharField(
        source="orden_compra.folio", read_only=True, default=None
    )
    op_folio = serializers.CharField(source="op.folio_op", read_only=True, default=None)
    orden_compra_tipo_compra = serializers.CharField(
        source="orden_compra.tipo_compra", read_only=True, default=None
    )
    # Anotados por ``_anotar_listado_recepciones`` (misma consulta del listado).
    piezas_recibidas = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True, coerce_to_string=False)
    renglones = serializers.IntegerField(read_only=True)
    inspeccion_estado = serializers.CharField(read_only=True, allow_null=True)
    piezas_aprobadas = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True, allow_null=True, coerce_to_string=False)
    piezas_rechazadas = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True, allow_null=True, coerce_to_string=False)
    usuario_nombre = serializers.SerializerMethodField()
    pedidos = serializers.SerializerMethodField()

    def get_usuario_nombre(self, obj):
        return _nombre_usuario(obj.usuario)

    def get_pedidos(self, obj):
        mapa = self.context.get("pedidos_por_recepcion")
        if mapa is None or obj.pk not in mapa:
            from compras.services.recepcion_pedidos import pedidos_de_recepciones
            mapa = pedidos_de_recepciones([obj])
        return mapa[obj.pk]

    class Meta(RecepcionSerializer.Meta):
        pass


class RecepcionDetalleRetrieveSerializer(RecepcionDetalleResumenSerializer):
    """Renglón anidado del ``retrieve`` individual de una recepción.

    Es la misma forma que el renglón de las recepciones anidadas en el retrieve
    de la OC (``RecepcionDetalleResumenSerializer``): hereda sus campos para que
    ambos detalles no diverjan. Se conserva como clase propia porque es el nombre
    que usa ``RecepcionRetrieveSerializer`` y el schema de OpenAPI.
    """


class RecepcionRetrieveSerializer(_OrigenSalidaMaquilaMixin, _CancelacionRecepcionMixin, serializers.ModelSerializer):
    """Detalle individual de una recepción (acción ``retrieve``).

    Forma dedicada, independiente de ``RecepcionListSerializer`` (que conserva la
    forma plana ``__all__`` que consume WMS/list): expone las FK resueltas
    (``estatus_label``, ``sucursal_nombre``…) y anida los renglones en
    ``detalles``. No incluye los ids ``orden_compra``/``op`` ni ``activo``; sí el
    folio del origen (EC-879), con los mismos nombres que el listado y el
    encuadre RFID: ``orden_compra_folio`` y ``op_folio`` vienen siempre y el que
    no corresponde a ``tipo_origen`` es ``null`` (``OrdenCompra.folio`` también
    puede ser nulo). Ambas FK van en el ``select_related`` del viewset: sin
    consultas extra. Se deja separada del chain de retrieve de OrdenCompra
    (``RecepcionRelacionadaSerializer``) para no acoplar ambos caminos.
    """

    estatus_label = serializers.CharField(source="get_estatus_display", read_only=True)
    orden_compra_folio = serializers.CharField(
        source="orden_compra.folio", read_only=True, default=None
    )
    op_folio = serializers.CharField(source="op.folio_op", read_only=True, default=None)
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
    # Solo el retrieve (EC-926): misma regla que ``cancelar/``, para quien consulta.
    puede_cancelar = serializers.SerializerMethodField()

    def get_puede_cancelar(self, obj) -> bool:
        from compras.services.cancelacion_recepcion import puede_cancelar_recepcion

        request = self.context.get("request")
        return bool(request) and puede_cancelar_recepcion(request.user, obj)

    class Meta:
        model = Recepcion
        fields = [
            "id",
            "tipo_origen",
            "orden_compra_folio",
            "op_folio",
            "salida_maquila",
            "salida_maquila_folio",
            "salida_maquila_origen",
            "salida_maquila_origen_folio",
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
            "motivo_cancelacion",
            "fecha_cancelacion",
            "cancelado_por",
            "cancelado_por_nombre",
            "puede_cancelar",
            "created_at",
            "updated_at",
            "detalles",
        ]
        read_only_fields = ["motivo_cancelacion", "fecha_cancelacion", "cancelado_por"]


class RecepcionOnboardingHeaderSerializer(serializers.Serializer):
    orden_compra = serializers.IntegerField(required=False, allow_null=True)
    orden_produccion = serializers.IntegerField(required=False, allow_null=True)
    # Regreso de maquila: con OP se recibe la OP; de OB/OR se cuentan las piezas
    # por renglón de la SM (sin inventario).
    salida_maquila = serializers.IntegerField(required=False, allow_null=True)
    # Cierra la SM aunque falten piezas: lo que no regresó queda como faltante.
    cerrar_salida_maquila = serializers.BooleanField(required=False, default=False)
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
    salida_maquila_detalle = serializers.IntegerField(required=False, allow_null=True)  # SM de OB/OR
    cantidad_recibida = serializers.DecimalField(max_digits=18, decimal_places=4)
    ubicacion = serializers.IntegerField(required=False, allow_null=True)
    lote = serializers.IntegerField(required=False, allow_null=True)
    serie = serializers.IntegerField(required=False, allow_null=True)


class RecepcionOnboardingSerializer(serializers.Serializer):
    recepcion = RecepcionOnboardingHeaderSerializer()
    detalle = RecepcionOnboardingDetalleInputSerializer(many=True)

    def validate_detalle(self, detalle):
        # EC-894: sin renglones, o con uno en cantidad <= 0, no hay recepción.
        # Se valida aquí, antes de consumir folio, crear la recepción o cambiar
        # el estatus de la OC/OP. El error va plano (``{"detalle": ["..."]}``)
        # y numera el renglón desde 1, igual que los demás de la vista.
        # Excepción: una SM de OB/OR sin renglones recibe todo lo pendiente;
        # la vista lo distingue y la de OP sigue exigiéndolos.
        if not detalle and not (self.initial_data.get("recepcion") or {}).get("salida_maquila"):
            raise serializers.ValidationError(
                "La recepción debe tener al menos un renglón con cantidad recibida."
            )
        for idx, item in enumerate(detalle):
            if item["cantidad_recibida"] <= 0:
                origen = item.get("orden_compra_detalle") and f"orden_compra_detalle {item['orden_compra_detalle']}" or (
                    item.get("orden_produccion_detalle") and f"orden_produccion_detalle {item['orden_produccion_detalle']}")
                raise serializers.ValidationError(  # #1188: con referencia estable
                    f"El renglón #{idx + 1}{f' ({origen})' if origen else ''} debe tener cantidad_recibida > 0."
                )
        return detalle


class CalidadInspeccionDetalleSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source="recepcion_detalle.articulo_nombre", read_only=True, default=None)

    class Meta:
        model = CalidadInspeccionDetalle
        fields = [
            "id",
            "recepcion_detalle",
            "producto_nombre",
            "cantidad_inspeccionada",
            "cantidad_aprobada",
            "cantidad_rechazada",
            "resultado",
            "motivo_rechazo",
        ]


class CalidadInspeccionSerializer(serializers.ModelSerializer):
    estado_label = serializers.CharField(source="get_estado_display", read_only=True)
    inspector_nombre = serializers.SerializerMethodField()
    # ``anulada_por`` va en el ``select_related`` del viewset (#1373).
    anulada_por_nombre = serializers.SerializerMethodField()
    recepcion_folio = serializers.CharField(source="recepcion.folio", read_only=True)
    detalles = CalidadInspeccionDetalleSerializer(many=True, read_only=True)
    pedido_id = serializers.SerializerMethodField()
    pedido_folio = serializers.SerializerMethodField()
    pedidos = serializers.SerializerMethodField()
    # EC-973: algo rechazado/en cuarentena aún sin devolver al proveedor (la regla de
    # ``devoluciones-proveedor/pendientes/``). El viewset lo anota; sin anotación
    # (respuesta de ``onboarding/``) se calcula con 1 consulta.
    tiene_devolucion_pendiente = serializers.SerializerMethodField()

    def get_tiene_devolucion_pendiente(self, obj):
        anotado = getattr(obj, "tiene_devolucion_pendiente", None)
        if anotado is not None:
            return anotado
        from compras.services.devolucion_proveedor import renglones_devolvibles

        return renglones_devolvibles().filter(calidad_inspeccion=obj.pk, cantidad_pendiente__gt=0).exists()

    def get_pedidos(self, obj):
        """Pedido(s) maestro(s) de la recepción; el listado los calcula en bloque."""
        from compras.services.recepcion_pedidos import pedidos_de_recepciones

        mapa = self.context.get("pedidos_por_recepcion")
        if mapa is None:
            mapa = pedidos_de_recepciones([obj.recepcion])
        return mapa.get(obj.recepcion_id, [])

    def get_pedido_id(self, obj):
        lista = self.get_pedidos(obj)
        return lista[0]["id"] if lista else None

    def get_pedido_folio(self, obj):
        lista = self.get_pedidos(obj)
        return lista[0]["folio"] if lista else None

    class Meta:
        model = CalidadInspeccion
        fields = [
            "id",
            "recepcion",
            "recepcion_folio",
            "pedido_id",
            "pedido_folio",
            "pedidos",
            "inspector",
            "inspector_nombre",
            "fecha",
            "estado",
            "estado_label",
            "observaciones",
            "detalles",
            "motivo_anulacion",
            "fecha_anulacion",
            "anulada_por",
            "anulada_por_nombre",
            "tiene_devolucion_pendiente",
        ]
        read_only_fields = ["motivo_anulacion", "fecha_anulacion", "anulada_por"]

    def get_anulada_por_nombre(self, obj):
        return _nombre_usuario(obj.anulada_por) if obj.anulada_por_id else None

    def get_inspector_nombre(self, obj):
        if not obj.inspector_id:
            return None
        return f"{obj.inspector.nombre} {obj.inspector.apellido_paterno}".strip()


class CalidadInspeccionDetalleInputSerializer(serializers.Serializer):
    # ``RESULTADO_CHOICES`` en el modelo trae ``"concesion lazzar"`` con
    # espacio (no ``concesion_lazzar``) — se respeta tal cual, no se corrige
    # aquí para no divergir del valor real que acepta la base.
    recepcion_detalle = serializers.IntegerField()
    cantidad_aprobada = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))
    cantidad_rechazada = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0"))
    resultado = serializers.ChoiceField(choices=CalidadInspeccionDetalle.RESULTADO_CHOICES)
    motivo_rechazo = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class CalidadInspeccionInputSerializer(serializers.Serializer):
    recepcion = serializers.IntegerField()
    inspector = serializers.IntegerField()
    fecha = serializers.DateField(required=False, allow_null=True)
    observaciones = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    detalle = CalidadInspeccionDetalleInputSerializer(many=True)


class RecepcionRFIDLecturaSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source="producto.nombre", read_only=True, default=None)
    producto_variante_nombre = serializers.CharField(
        source="producto_variante.nombre", read_only=True, default=None
    )

    class Meta:
        model = RecepcionRFIDLectura
        fields = [
            "id", "codigo_tag", "orden_compra_detalle", "orden_produccion_detalle",
            "producto", "producto_nombre", "producto_variante", "producto_variante_nombre",
            "cantidad_leida", "metadata", "created_at",
        ]
        read_only_fields = [
            "producto", "producto_variante", "orden_compra_detalle",
            "orden_produccion_detalle", "metadata",
        ]


class RecepcionRFIDEncuadreSerializer(_CancelacionRecepcionMixin, serializers.ModelSerializer):
    orden_compra_folio = serializers.CharField(source="orden_compra.folio", read_only=True, default=None)
    op_folio = serializers.CharField(source="op.folio_op", read_only=True, default=None)
    proveedor_nombre = serializers.CharField(source="proveedor.nombre", read_only=True, default=None)
    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True, default=None)
    estatus_label = serializers.CharField(source="get_estatus_display", read_only=True)
    lecturas = RecepcionRFIDLecturaSerializer(many=True, read_only=True)
    resumen = serializers.SerializerMethodField()
    # ``usuario``/``aceptado_por``/``cancelado_por`` van en el ``select_related`` del viewset (#1373, #1440).
    usuario_nombre = serializers.SerializerMethodField()
    aceptado_por_nombre = serializers.SerializerMethodField()

    class Meta:
        model = RecepcionRFIDEncuadre
        fields = [
            "id", "tipo_origen", "orden_compra", "orden_compra_folio", "op", "op_folio",
            "recepcion", "empresa", "sucursal", "proveedor", "proveedor_nombre",
            "almacen", "almacen_nombre", "usuario", "usuario_nombre", "serie_codigo", "fecha_recepcion",
            "remision", "factura_referencia", "observaciones", "estatus", "estatus_label",
            "activo", "created_at", "updated_at", "lecturas", "resumen",
            "aceptado_por", "aceptado_por_nombre", "fecha_aceptacion", "motivo_cancelacion", "fecha_cancelacion",
            "cancelado_por", "cancelado_por_nombre",
        ]
        read_only_fields = [
            "empresa", "sucursal", "proveedor", "usuario", "recepcion", "estatus", "activo",
            "aceptado_por", "fecha_aceptacion", "motivo_cancelacion", "fecha_cancelacion", "cancelado_por",
        ]

    def get_usuario_nombre(self, obj):
        return _nombre_usuario(obj.usuario) if obj.usuario_id else None

    def get_aceptado_por_nombre(self, obj):
        return _nombre_usuario(obj.aceptado_por) if obj.aceptado_por_id else None

    def get_resumen(self, obj):
        from compras.api.views import resumen_encuadre_rfid
        # El listado manda los renglones y el recibido en bloque (EC-778); el
        # detalle, create y aceptar los calculan para su único encuadre.
        return resumen_encuadre_rfid(obj, self.context.get("resumen_mapas"))


class RecepcionRFIDEncuadreListSerializer(RecepcionRFIDEncuadreSerializer):
    """Fila del listado (EC-981): los campos del detalle sin ``lecturas[]`` ni
    ``resumen``, con sus totales planos (mismos nombres y formato que en
    ``resumen``). ``total_leido``/``total_sin_asignar`` vienen anotados por el
    viewset; ``total_esperado``, en ``context["totales_esperados"]``."""

    lecturas = None
    resumen = None
    total_esperado = serializers.SerializerMethodField()
    total_leido = serializers.SerializerMethodField()
    total_sin_asignar = serializers.SerializerMethodField()
    completo = serializers.SerializerMethodField()

    class Meta(RecepcionRFIDEncuadreSerializer.Meta):
        fields = [
            *(f for f in RecepcionRFIDEncuadreSerializer.Meta.fields if f not in ("lecturas", "resumen")),
            "total_esperado", "total_leido", "total_sin_asignar", "completo",
        ]

    def _esperado(self, obj):
        return self.context["totales_esperados"].get(obj.pk, Decimal("0"))

    @extend_schema_field(OpenApiTypes.STR)
    def get_total_esperado(self, obj):
        return str(self._esperado(obj))

    @extend_schema_field(OpenApiTypes.STR)
    def get_total_leido(self, obj):
        return str(obj.total_leido)

    @extend_schema_field(OpenApiTypes.STR)
    def get_total_sin_asignar(self, obj):
        return str(obj.total_sin_asignar)

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_completo(self, obj):
        esperado = self._esperado(obj)
        return bool(esperado > 0 and obj.total_leido >= esperado)


class RecepcionRFIDEncuadreCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecepcionRFIDEncuadre
        fields = [
            "tipo_origen", "orden_compra", "op", "almacen",
            "serie_codigo", "remision", "factura_referencia", "observaciones",
        ]


class RecepcionRFIDLecturaInputSerializer(serializers.Serializer):
    codigo_tag = serializers.CharField()



class OrdenCompraRutaCriticaSerializer(serializers.ModelSerializer):
    """Fecha real de recibo y comentario; ``oc_id`` solo de lectura."""

    oc_id = serializers.IntegerField(read_only=True)
    comentarios = serializers.CharField(required=False, allow_null=True, allow_blank=True, max_length=2000)

    class Meta:
        model = OrdenCompraRutaCritica
        fields = ["oc_id", "fecha_real_recibo", "comentarios", "updated_at"]
        read_only_fields = ["updated_at"]

    def validate_comentarios(self, value):
        return (value or "").strip() or None
