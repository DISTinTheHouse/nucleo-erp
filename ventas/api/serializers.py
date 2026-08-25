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
    cantidad_asignada_picking = serializers.SerializerMethodField()
    cantidad_surtida_picking = serializers.SerializerMethodField()

    class Meta:
        model = PedidoDetalleTalla
        fields = "__all__"

    def get_variante_nombre(self, obj):
        if not obj.variante_id:
            return None
        return obj.variante.nombre or obj.variante.sku

    def _tracking_context(self):
        return self.context.get("_picking_tracking") or {}

    def get_cantidad_asignada_picking(self, obj):
        ctx = self._tracking_context()
        m = ctx.get("asignado_map")
        if not m:
            return "0"
        return str(m.get(obj.id, 0))

    def get_cantidad_surtida_picking(self, obj):
        ctx = self._tracking_context()
        m = ctx.get("surtido_map")
        if not m:
            return "0"
        return str(m.get(obj.id, 0))

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
    tallas = serializers.SerializerMethodField()
    cantidad_total = serializers.SerializerMethodField()
    tracker_picking = serializers.SerializerMethodField()

    class Meta:
        model = PedidoDetalle
        fields = "__all__"

    def get_tallas(self, obj):
        # Reusamos la clase PedidoDetalleTallaReadSerializer pero le inyectamos
        # el contexto compartido de tracking (mapas de asignado/surtido) para
        # evitar N queries y reutilizar el snapshot de una sola pasada.
        #
        # ``obj.tallas.all()`` SIN ``.order_by()``: el orden por ``id`` ya lo
        # impone el ``Prefetch`` anidado de ``_pedido_detalles_prefetch()``.
        # Encadenar aquí un ``.order_by("id")`` clonaba el queryset, descartaba
        # ``_result_cache`` y re-ejecutaba la consulta UNA VEZ POR RENGLÓN
        # (N+1), tirando a la basura el prefetch del viewset.
        ctx_safe = dict(self.context or {})
        child = PedidoDetalleTallaReadSerializer(
            instance=obj.tallas.all(),
            many=True,
            context=ctx_safe,
        )
        return child.data

    def get_cantidad_total(self, obj):
        return sum(int(t.cantidad or 0) for t in obj.tallas.all())

    def get_tracker_picking(self, obj):
        from wms.services.picking_pipeline.pendientes import armar_tracker_linea
        ctx = self.context.get("_picking_tracking") or {}
        asignado_map = ctx.get("asignado_map") or {}
        surtido_map = ctx.get("surtido_map") or {}
        try:
            return armar_tracker_linea(obj, asignado_map, surtido_map)
        except Exception:
            return {
                "pct_asignado_linea": "0.0000",
                "pct_surtido_linea": "0.0000",
                "total_prendas_linea": "0",
                "total_asignado_linea": "0",
                "total_surtido_linea": "0",
            }

class PedidoListSerializer(serializers.ModelSerializer):
    """Serializer minimalista para el LISTADO de pedidos.

    Devuelve sólo los campos escalares que consume la tabla del frontend, sin
    ``detalles``/``tallas``/``servicios_extras``/``documentos``. Estos anidados
    (y sus prefetch) sólo se necesitan en el detalle (``retrieve``), no en la
    lista — evitar serializarlos en cada renglón del listado es lo que baja el
    tiempo de respuesta de ~15s a <1s. Campos explícitos, no ``__all__``.
    """

    class Meta:
        model = Pedido
        fields = [
            "id",
            "folio",
            "folio_consecutivo",
            "oc",
            "cliente_razon_social",
            "cliente_nombre",
            "gran_total",
            "subtotal",
            "created_at",
            "fecha_confirmacion",
            "estatus",
            "activo",
            "cliente",
            "moneda",
        ]

class PedidoSerializer(serializers.ModelSerializer):
    folio = serializers.CharField(read_only=True)
    folio_consecutivo = serializers.IntegerField(read_only=True)
    servicios_extras = serializers.SerializerMethodField()
    documentos = serializers.SerializerMethodField()
    tracker_picking = serializers.SerializerMethodField()
    folios_picking = serializers.SerializerMethodField()
    # Solo lectura: no cambia el contrato de escritura de ningún endpoint de
    # Pedido (POST/PATCH ignoran ``detalles``).
    detalles = serializers.SerializerMethodField()

    def get_servicios_extras(self, obj):
        # Sin ``.order_by("id")``: el orden lo impone el ``Prefetch`` del
        # viewset (``_pedido_servicios_extras_prefetch()``); encadenarlo aquí
        # clonaba el queryset e invalidaba esa caché. Si el pedido llega sin
        # prefetch (p.ej. la respuesta de un POST recién creado, sin renglones)
        # ``.all()`` consulta igual que antes.
        try:
            qs = obj.servicios_extras.all()
        except Exception:
            return []
        return PedidoServicioExtraSerializer(qs, many=True).data

    def get_documentos(self, obj):
        from ventas.services.pedido_documentos_service import listar_documentos_pedido
        try:
            return listar_documentos_pedido(obj)
        except Exception:
            return []

    def get_tracker_picking(self, obj):
        from wms.services.picking_pipeline.pendientes import armar_tracker_pedido
        try:
            # ``retrieve()`` ya calculó los mapas históricos una sola vez y los
            # dejó en el contexto; se los pasamos para que el tracker no repita
            # la agregación sobre ``PickingDetalle``. Si no hay contexto (POST /
            # PATCH / mesa-control) va ``None`` y el tracker los calcula solo.
            return armar_tracker_pedido(
                obj, picking_maps=self.context.get("_picking_tracking")
            )
        except Exception:
            return {
                "pct_asignado_pedido": "0.0000",
                "pct_surtido_pedido": "0.0000",
                "total_prendas_pedido": "0",
                "total_asignado": "0",
                "total_surtido": "0",
            }

    def get_folios_picking(self, obj):
        from wms.services.picking_pipeline.pendientes import listar_folios_picking
        try:
            return listar_folios_picking(obj)
        except Exception:
            return []

    def get_detalles(self, obj):
        # ``PedidoViewSet.retrieve()`` construye ``_picking_tracking`` en el
        # serializer.context con {asignado_map, surtido_map} una sola vez por
        # pedido. Aquí lo propagamos a los hijos sin recalcular:
        ctx_safe = dict(self.context or {})
        if "_picking_tracking" not in ctx_safe:
            from wms.services.picking_pipeline.pendientes import historical_maps
            try:
                asignado_map, surtido_map = historical_maps(obj)
                ctx_safe["_picking_tracking"] = {
                    "asignado_map": dict(asignado_map),
                    "surtido_map": dict(surtido_map),
                }
            except Exception:
                ctx_safe["_picking_tracking"] = {"asignado_map": {}, "surtido_map": {}}
        # ``obj.detalles.all()`` SIN ``.order_by()``: ``_pedido_detalles_prefetch()``
        # ya ordena por ``id`` los renglones y sus tallas. El ``.order_by("id")``
        # que había aquí clonaba el queryset, descartaba ``_result_cache`` y
        # re-ejecutaba tanto la consulta de ``detalles`` como su prefetch anidado
        # de ``tallas``.
        child = PedidoDetalleReadSerializer(
            instance=obj.detalles.all(),
            many=True,
            context=ctx_safe,
        )
        return child.data

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

    def validate_pedido(self, pedido):
        # Aislamiento multi-tenant en ESCRITURA (POST/PATCH). ``get_queryset`` del
        # ViewSet sólo acota LECTURAS; con ``fields='__all__'`` el campo ``pedido``
        # acepta cualquier ``Pedido`` (``queryset=Pedido.objects.all()``), así que
        # sin esto un usuario podría crear un renglón —o mover el suyo— hacia el
        # pedido de otra empresa. Misma convención empresa-only que
        # ``PedidoDetalleViewSet.get_queryset()`` y ``SerieFolioSerializer.validate``
        # (nucleo): superuser puede todo; sin empresa no se permite; el resto sólo
        # su empresa.
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if getattr(user, "is_superuser", False):
            return pedido
        empresa = getattr(user, "empresa", None)
        if empresa is None or pedido.empresa_id != empresa.pk:
            raise serializers.ValidationError(
                "El pedido no pertenece a la empresa del usuario."
            )
        return pedido

    class Meta:
        model = PedidoDetalle
        fields = '__all__'

class PedidoDetalleTallaSerializer(serializers.ModelSerializer):
    pedido_folio = serializers.CharField(source='pedido_detalle.pedido.folio', read_only=True)

    def validate_pedido_detalle(self, pedido_detalle):
        # Aislamiento multi-tenant en ESCRITURA vía la cadena ``pedido_detalle`` ->
        # ``pedido`` -> ``Pedido.empresa`` (misma cadena que
        # ``PedidoDetalleTallaViewSet.get_queryset()``). Ver la nota en
        # ``PedidoDetalleSerializer.validate_pedido``.
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if getattr(user, "is_superuser", False):
            return pedido_detalle
        empresa = getattr(user, "empresa", None)
        if empresa is None or pedido_detalle.pedido.empresa_id != empresa.pk:
            raise serializers.ValidationError(
                "El renglón de pedido no pertenece a la empresa del usuario."
            )
        return pedido_detalle

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
