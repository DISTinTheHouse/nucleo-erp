from decimal import Decimal

from rest_framework import serializers

from catalogo.models import Producto, ProductoVariante
from logistica.models import Envio
from wms.models import (
    Despacho,
    DespachoDetalle,
    EtiquetaRFIDDetalle,
    EtiquetaRFIDImpresion,
    Packing,
    PackingDetalle,
    Picking,
    PickingDetalle,
    PickingOrdenTrabajo,
    Transferencia,
    TransferenciaDetalle,
)

class TransferenciaDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransferenciaDetalle
        fields = "__all__"
        read_only_fields = ["transferencia"]

    def validate(self, attrs):
        producto = attrs.get("producto")
        producto_variante = attrs.get("producto_variante")

        if not producto and not producto_variante:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Debe proporcionar 'producto' o 'producto_variante'."
                    ]
                }
            )

        if producto and producto_variante:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Solo puede proporcionar 'producto' o 'producto_variante'."
                    ]
                }
            )

        return attrs


class TransferenciaSerializer(serializers.ModelSerializer):
    transferencia_detalle = TransferenciaDetalleSerializer(many=True)

    class Meta:
        model = Transferencia
        fields = "__all__"
        read_only_fields = ["empresa", "sucursal", "folio", "usuario", "status"]

    def validate(self, attrs):
        almacen_origen = attrs.get("almacen_origen")
        almacen_destino = attrs.get("almacen_destino")

        if almacen_origen == almacen_destino:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "El almacen de origen y destino no pueden ser iguales."
                    ]
                }
            )

        return attrs


class TransferenciaDetalleReadSerializer(serializers.ModelSerializer):
    """Renglón anidado del ``retrieve`` individual de una transferencia.

    Resuelve los nombres a través de las FK que el ``prefetch_related`` del viewset
    ya trae con ``select_related`` — sin consultas por fila.

    ``producto`` y ``producto_variante`` son mutuamente excluyentes (lo garantiza
    ``TransferenciaDetalleSerializer.validate``), así que exactamente uno de
    ``producto_nombre``/``producto_variante_nombre`` resuelve y el otro queda en
    ``null``.

    ``ubicacion_origen``/``ubicacion_destino`` se resuelven con ``str(ubicacion)``
    —siguiendo la convención de ``reporte-movimientos-periodo``— porque
    ``Ubicacion`` NO tiene campo ``nombre``: su etiqueta se compone de
    ``almacen.nombre`` + coordenadas (pasillo-rack-nivel-posicion), de ahí el
    ``select_related`` de ``ubicacion_*__almacen`` en el viewset.

    ``lote``/``serie`` se exponen solo como id: ambos modelos únicamente tienen
    ``id`` y una FK a ``producto``, sin ningún campo identificador que resolver.
    """

    producto_nombre = serializers.CharField(
        source="producto.nombre", read_only=True, default=None
    )
    producto_variante_nombre = serializers.CharField(
        source="producto_variante.nombre", read_only=True, default=None
    )
    ubicacion_origen_nombre = serializers.SerializerMethodField()
    ubicacion_destino_nombre = serializers.SerializerMethodField()

    class Meta:
        model = TransferenciaDetalle
        fields = [
            "id",
            "producto",
            "producto_nombre",
            "producto_variante",
            "producto_variante_nombre",
            "cantidad",
            "ubicacion_origen",
            "ubicacion_origen_nombre",
            "ubicacion_destino",
            "ubicacion_destino_nombre",
            "lote",
            "serie",
        ]

    def get_ubicacion_origen_nombre(self, obj):
        return str(obj.ubicacion_origen) if obj.ubicacion_origen_id else None

    def get_ubicacion_destino_nombre(self, obj):
        return str(obj.ubicacion_destino) if obj.ubicacion_destino_id else None


class TransferenciaListSerializer(serializers.ModelSerializer):
    """Listado de transferencias (acción ``list``).

    Forma ligera y plana: encabezado con las FK resueltas, sin anidar renglones
    —mismo criterio que ``RecepcionListSerializer`` vs. ``RecepcionRetrieveSerializer``
    en Compras—. ``almacen_origen``/``almacen_destino``/``usuario`` son NOT NULL en
    el modelo, así que sus nombres siempre resuelven.
    """

    almacen_origen_nombre = serializers.CharField(
        source="almacen_origen.nombre", read_only=True
    )
    almacen_destino_nombre = serializers.CharField(
        source="almacen_destino.nombre", read_only=True
    )
    usuario_nombre = serializers.SerializerMethodField()

    class Meta:
        model = Transferencia
        fields = [
            "id",
            "folio",
            "status",
            "observaciones",
            "fecha_creacion",
            "almacen_origen",
            "almacen_origen_nombre",
            "almacen_destino",
            "almacen_destino_nombre",
            "usuario",
            "usuario_nombre",
        ]

    def get_usuario_nombre(self, obj):
        usuario = obj.usuario
        if not usuario:
            return None
        # Mismo fallback que ``reporte-movimientos-periodo``: nombre completo y,
        # si el usuario no tiene first/last name, el email.
        return usuario.get_full_name().strip() or usuario.email


class TransferenciaRetrieveSerializer(TransferenciaListSerializer):
    """Detalle individual de una transferencia (acción ``retrieve``).

    Mismo encabezado que el listado más los renglones anidados.
    """

    transferencia_detalle = TransferenciaDetalleReadSerializer(
        many=True, read_only=True
    )

    class Meta(TransferenciaListSerializer.Meta):
        fields = TransferenciaListSerializer.Meta.fields + ["transferencia_detalle"]


class PickingDetalleSerializer(serializers.ModelSerializer):
    """Renglón anidado de un picking.

    Resuelve los nombres a través de las FK que el ``prefetch_related`` del viewset
    ya trae con ``select_related`` — sin consultas por fila, misma convención que
    ``TransferenciaDetalleReadSerializer``.

    ``producto``/``producto_variante``: el que esté en ``null`` deja su
    ``*_nombre`` en ``null``.

    ``ubicacion`` se resuelve con ``str(ubicacion)`` porque ``Ubicacion`` NO tiene
    campo ``nombre``: su etiqueta se compone de ``almacen.nombre`` + coordenadas
    (pasillo-rack-nivel-posicion), de ahí el ``select_related`` de
    ``ubicacion__almacen`` en el viewset.

    ``lote`` (``inventarios.Lote``) se expone solo como id: el modelo únicamente
    tiene ``id`` y una FK a ``producto``, sin ningún campo identificador que
    resolver (misma convención que ``lote``/``serie`` en transferencias).

    ``oleada`` y ``lote_picking`` usan ``__str__`` null-safe como etiqueta porque
    sus modelos no tienen un campo ``nombre`` natural (su ``__str__`` compone id
    + estado), al igual que ``TransferenciaDetalleReadSerializer`` con ``Ubicacion``.
    """

    producto_nombre = serializers.CharField(
        source="producto.nombre", read_only=True, default=None
    )
    producto_variante_nombre = serializers.CharField(
        source="producto_variante.nombre", read_only=True, default=None
    )
    talla_id = serializers.IntegerField(
        source="pedido_detalle_talla.variante.talla_id", read_only=True
    )
    talla_nombre = serializers.CharField(
        source="pedido_detalle_talla.variante.talla.nombre", read_only=True, default=None
    )
    ubicacion_nombre = serializers.SerializerMethodField()
    operador_nombre = serializers.SerializerMethodField()

    class Meta:
        model = PickingDetalle
        fields = "__all__"
        read_only_fields = [
            "picking",
            "producto",
            "producto_variante",
            "cantidad_solicitada",
        ]

    def get_ubicacion_nombre(self, obj):
        return str(obj.ubicacion) if obj.ubicacion_id else None

    def get_operador_nombre(self, obj):
        operador = obj.operador
        if not operador:
            return None
        return operador.get_full_name().strip() or operador.email


class PickingOrdenTrabajoReadSerializer(serializers.ModelSerializer):
    """Renglón de vínculo entre un picking y una orden de trabajo generada.

    Se adjunta nested en el list/retrieve de ``Picking`` para que Next.js pueda
    recuperar la relación sin un endpoint adicional —la relación de Django se
    prefetchcea desde ``PickingViewSet.get_queryset``.
    """

    orden_bordado_folio = serializers.CharField(
        source="orden_bordado.folio_bordado", read_only=True, default=None
    )
    orden_reflejante_folio = serializers.CharField(
        source="orden_reflejante.folio_reflejante", read_only=True, default=None
    )
    orden_corte_manga_folio = serializers.CharField(
        source="orden_corte_manga.folio_ocm", read_only=True, default=None
    )
    tipo_orden_label = serializers.CharField(
        source="get_tipo_orden_display", read_only=True
    )

    class Meta:
        model = PickingOrdenTrabajo
        fields = [
            "id",
            "tipo_orden",
            "tipo_orden_label",
            "orden_bordado",
            "orden_bordado_folio",
            "orden_reflejante",
            "orden_reflejante_folio",
            "orden_corte_manga",
            "orden_corte_manga_folio",
        ]


class PickingSerializer(serializers.ModelSerializer):
    """Serializer compartido de picking (``list``, ``retrieve`` y respuesta del
    ``create``).

    Además de las FK crudas expone los nombres resueltos —misma convención que
    ``TransferenciaListSerializer``—. Todos los ``*_nombre`` son de solo lectura,
    así que el contrato de escritura del ``POST /pickings/`` no cambia.

    ``pedido``/``operador``/``almacen``/``almacen_destino``/``usuario`` son NOT NULL
    en el modelo (``pedido_folio`` aun así puede ser ``null``: ``Pedido.folio`` es
    nullable). ``oleada``/``zona_almacen``/``lote`` son FK opcionales y resuelven
    a ``null`` cuando faltan. ``Oleada`` y ``LotePicking`` no tienen campo
    ``nombre``: su etiqueta se compone en ``__str__`` (id + estado), por eso se
    resuelven con ``str()`` null-safe, igual que ``Ubicacion`` en transferencias.

    ``ordenes_trabajo`` está nested y prefetchceado para que el GET recupere la
    trazabilidad contra producción; el POST además inyecta un arreglo plano
    ``ordenes_trabajo_generadas`` con resumen ``{tipo, id, folio}``.
    """

    picking_detalle = PickingDetalleSerializer(many=True)
    ordenes_trabajo = PickingOrdenTrabajoReadSerializer(many=True, read_only=True)

    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True)
    operador_nombre = serializers.SerializerMethodField()
    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True)
    almacen_destino_nombre = serializers.CharField(
        source="almacen_destino.nombre", read_only=True, default=None
    )
    usuario_nombre = serializers.SerializerMethodField()
    oleada_nombre = serializers.SerializerMethodField()
    zona_almacen_nombre = serializers.CharField(
        source="zona_almacen.nombre", read_only=True, default=None
    )
    lote_nombre = serializers.SerializerMethodField()

    class Meta:
        model = Picking
        fields = "__all__"
        read_only_fields = [
            "folio",
            "total_lineas",
            "total_lineas_completas",
            "usuario",
            "created_at",
            "updated_at",
            "empresa",
            "sucursal",
        ]

    def _nombre_usuario(self, usuario):
        if not usuario:
            return None
        # Mismo fallback que ``TransferenciaListSerializer.usuario_nombre``:
        # nombre completo y, si el usuario no tiene first/last name, el email.
        return usuario.get_full_name().strip() or usuario.email

    def get_operador_nombre(self, obj):
        return self._nombre_usuario(obj.operador)

    def get_usuario_nombre(self, obj):
        return self._nombre_usuario(obj.usuario)

    def get_oleada_nombre(self, obj):
        return str(obj.oleada) if obj.oleada_id else None

    def get_lote_nombre(self, obj):
        return str(obj.lote) if obj.lote_id else None


class PickingCreateSerializer(serializers.ModelSerializer):
    """Alta de picking parcial.

    El frontend envía encabezado + las cantidades reales por línea/talla que se
    surtirán en este picking. El backend valida contra el pendiente calculado a
    partir del historial de ``PickingDetalle``.
    """

    class PickingCreateDetalleInputSerializer(serializers.Serializer):
        pedido_detalle_talla = serializers.IntegerField(min_value=1)
        cantidad_asignada = serializers.DecimalField(
            max_digits=18,
            decimal_places=4,
            min_value=Decimal("0.0001"),
        )
        observaciones = serializers.CharField(required=False, allow_blank=True)
        generar_orden_bordado = serializers.BooleanField(required=False, default=False)
        generar_orden_reflejante = serializers.BooleanField(required=False, default=False)
        generar_orden_corte_manga = serializers.BooleanField(required=False, default=False)

    picking_detalle = PickingCreateDetalleInputSerializer(many=True)

    class Meta:
        model = Picking
        fields = [
            "pedido",
            "operador",
            "almacen",
            "almacen_destino",
            "oleada",
            "zona_almacen",
            "lote",
            "prioridad",
            "tipo",
            "fecha_inicio",
            "fecha_fin",
            "fecha_limite",
            "observaciones",
            "picking_detalle",
        ]

class PackingDetalleReadSerializer(serializers.ModelSerializer):
    producto = serializers.IntegerField(
        source="picking_detalle.producto_id", read_only=True
    )
    producto_nombre = serializers.CharField(
        source="picking_detalle.producto.nombre", read_only=True, default=None
    )
    producto_variante = serializers.IntegerField(
        source="picking_detalle.producto_variante_id", read_only=True
    )
    producto_variante_nombre = serializers.CharField(
        source="picking_detalle.producto_variante.nombre", read_only=True, default=None
    )
    pedido_detalle = serializers.IntegerField(
        source="picking_detalle.pedido_detalle_id", read_only=True
    )
    pedido_detalle_talla = serializers.IntegerField(
        source="picking_detalle.pedido_detalle_talla_id", read_only=True
    )
    talla_id = serializers.IntegerField(
        source="picking_detalle.pedido_detalle_talla.variante.talla_id", read_only=True
    )
    talla_nombre = serializers.CharField(
        source="picking_detalle.pedido_detalle_talla.variante.talla.nombre",
        read_only=True,
        default=None,
    )
    cantidad_asignada = serializers.DecimalField(
        source="picking_detalle.cantidad_asignada",
        max_digits=18,
        decimal_places=4,
        read_only=True,
    )
    cantidad_solicitada = serializers.DecimalField(
        source="picking_detalle.cantidad_solicitada",
        max_digits=18,
        decimal_places=4,
        read_only=True,
    )
    cantidad_surtida = serializers.DecimalField(
        source="picking_detalle.cantidad_surtida",
        max_digits=18,
        decimal_places=4,
        read_only=True,
    )
    ubicacion = serializers.IntegerField(
        source="picking_detalle.ubicacion_id", read_only=True
    )
    ubicacion_nombre = serializers.SerializerMethodField()
    caja_numero = serializers.IntegerField(source="caja.numero", read_only=True, default=None)

    class Meta:
        model = PackingDetalle
        fields = "__all__"
        read_only_fields = ["packing"]

    def get_ubicacion_nombre(self, obj):
        return str(obj.picking_detalle.ubicacion) if obj.picking_detalle.ubicacion_id else None

class PackingSerializer(serializers.ModelSerializer):
    packing_detalle = PackingDetalleReadSerializer(many=True, read_only=True)
    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True)
    picking_folio = serializers.CharField(source="picking.folio", read_only=True)
    picking_estado = serializers.CharField(source="picking.estado", read_only=True)
    picking_almacen = serializers.IntegerField(source="picking.almacen_id", read_only=True)
    picking_almacen_nombre = serializers.CharField(
        source="picking.almacen.nombre", read_only=True, default=None
    )
    operador_nombre = serializers.SerializerMethodField()
    usuario_nombre = serializers.SerializerMethodField()

    class Meta:
        model = Packing
        fields = "__all__"
        read_only_fields = [
            "folio", 
            "empresa", 
            "sucursal", 
            "pedido", 
            "operador", 
            "usuario", 
            "created_at", 
            "updated_at",
        ]

        
    def _nombre_usuario(self, usuario):
        if not usuario:
            return None
        return usuario.get_full_name().strip() or usuario.email

    def get_operador_nombre(self, obj):
        return self._nombre_usuario(obj.operador)
    
    def get_usuario_nombre(self, obj):
        return self._nombre_usuario(obj.usuario)


class PackingCreateSerializer(serializers.ModelSerializer):
    """Alta de packing guiada por picking.

    El frontend envía el ``picking`` origen y las líneas concretas que va a
    empacar. El backend valida contra lo ya registrado históricamente para
    evitar sobre-empaque.
    """

    class PackingCreateDetalleInputSerializer(serializers.Serializer):
        picking_detalle = serializers.IntegerField(min_value=1)
        cantidad_empacada = serializers.DecimalField(
            max_digits=18,
            decimal_places=4,
            min_value=Decimal("0.0001"),
        )
        observaciones = serializers.CharField(required=False, allow_blank=True)

    packing_detalle = PackingCreateDetalleInputSerializer(many=True)

    class Meta:
        model = Packing
        fields = [
            "picking",
            "numero_cajas",
            "peso_total",
            "volumen_total",
            "fecha_inicio",
            "fecha_fin",
            "observaciones",
            "packing_detalle",
        ]


class DespachoDetalleReadSerializer(serializers.ModelSerializer):
    picking_detalle = serializers.IntegerField(
        source="packing_detalle.picking_detalle_id", read_only=True
    )
    pedido_detalle = serializers.IntegerField(
        source="packing_detalle.picking_detalle.pedido_detalle_id", read_only=True
    )
    pedido_detalle_talla = serializers.IntegerField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla_id", read_only=True
    )
    producto = serializers.IntegerField(
        source="packing_detalle.picking_detalle.producto_id", read_only=True
    )
    producto_nombre = serializers.CharField(
        source="packing_detalle.picking_detalle.producto.nombre", read_only=True, default=None
    )
    producto_variante = serializers.IntegerField(
        source="packing_detalle.picking_detalle.producto_variante_id", read_only=True
    )
    producto_variante_nombre = serializers.CharField(
        source="packing_detalle.picking_detalle.producto_variante.nombre",
        read_only=True,
        default=None,
    )
    talla_id = serializers.IntegerField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla.variante.talla_id",
        read_only=True,
    )
    talla_nombre = serializers.CharField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla.variante.talla.nombre",
        read_only=True,
        default=None,
    )
    color_id = serializers.IntegerField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla.variante.color_id",
        read_only=True,
    )
    color_nombre = serializers.CharField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla.variante.color.nombre",
        read_only=True,
        default=None,
    )
    ubicacion = serializers.IntegerField(
        source="packing_detalle.picking_detalle.ubicacion_id", read_only=True
    )
    ubicacion_nombre = serializers.SerializerMethodField()
    caja = serializers.IntegerField(source="packing_detalle.caja_id", read_only=True)
    caja_numero = serializers.IntegerField(
        source="packing_detalle.caja.numero", read_only=True, default=None
    )
    cantidad_empacada = serializers.DecimalField(
        source="packing_detalle.cantidad_empacada",
        max_digits=18,
        decimal_places=4,
        read_only=True,
    )
    estado = serializers.CharField(source="packing_detalle.estado", read_only=True)

    class Meta:
        model = DespachoDetalle
        fields = "__all__"
        read_only_fields = ["despacho"]

    def get_ubicacion_nombre(self, obj):
        ubicacion = getattr(obj.packing_detalle.picking_detalle, "ubicacion", None)
        return str(ubicacion) if ubicacion else None


class DespachoSerializer(serializers.ModelSerializer):
    despacho_detalle = DespachoDetalleReadSerializer(many=True, read_only=True)
    packing_folio = serializers.CharField(source="packing.folio", read_only=True)
    packing_estado = serializers.CharField(source="packing.estado", read_only=True)
    pedido = serializers.IntegerField(source="packing.pedido_id", read_only=True)
    pedido_folio = serializers.CharField(source="packing.pedido.folio", read_only=True)
    cliente = serializers.IntegerField(source="packing.pedido.cliente_id", read_only=True)
    cliente_nombre = serializers.CharField(
        source="packing.pedido.cliente.nombre", read_only=True, default=None
    )
    sucursal = serializers.IntegerField(source="packing.sucursal_id", read_only=True)
    sucursal_nombre = serializers.CharField(
        source="packing.sucursal.nombre", read_only=True, default=None
    )
    envio_transportista = serializers.IntegerField(
        source="envio.transportista_id", read_only=True, default=None
    )
    envio_transportista_nombre = serializers.CharField(
        source="envio.transportista.nombre", read_only=True, default=None
    )

    class Meta:
        model = Despacho
        fields = "__all__"


class DespachoCreateSerializer(serializers.ModelSerializer):
    class DespachoCreateDetalleInputSerializer(serializers.Serializer):
        packing_detalle = serializers.IntegerField(min_value=1)

    envio = serializers.PrimaryKeyRelatedField(
        queryset=Envio.objects.all(),
        required=False,
        allow_null=True,
    )
    despacho_detalle = DespachoCreateDetalleInputSerializer(many=True)

    class Meta:
        model = Despacho
        fields = [
            "packing",
            "envio",
            "despacho_detalle",
        ]


class EtiquetaRFIDDetalleReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = EtiquetaRFIDDetalle
        fields = "__all__"
        read_only_fields = ["impresion"]


class EtiquetaRFIDSerializer(serializers.ModelSerializer):
    folio = serializers.CharField(read_only=True)
    etiquetas = EtiquetaRFIDDetalleReadSerializer(many=True, read_only=True)
    empresa = serializers.IntegerField(source="empresa_id", read_only=True)
    sucursal = serializers.IntegerField(source="sucursal_id", read_only=True, allow_null=True)
    usuario = serializers.IntegerField(source="usuario_id", read_only=True, allow_null=True)
    producto = serializers.IntegerField(source="producto_id", read_only=True, allow_null=True)
    producto_variante = serializers.IntegerField(
        source="producto_variante_id", read_only=True, allow_null=True
    )
    producto_nombre = serializers.CharField(
        source="producto.nombre", read_only=True, default=None
    )
    producto_variante_nombre = serializers.CharField(
        source="producto_variante.nombre", read_only=True, default=None
    )
    sku = serializers.CharField(
        source="producto_variante.sku", read_only=True, default=None
    )
    codigo_producto = serializers.CharField(
        source="producto.codigo", read_only=True, default=None
    )

    class Meta:
        model = EtiquetaRFIDImpresion
        fields = "__all__"


class EtiquetaRFIDCreateSerializer(serializers.Serializer):
    class EtiquetaRFIDDetalleInputSerializer(serializers.Serializer):
        epc = serializers.CharField(max_length=64, required=False, allow_blank=True)
        barcode_value = serializers.CharField(
            max_length=128, required=False, allow_blank=True
        )
        serial = serializers.CharField(
            max_length=64, required=False, allow_blank=True, allow_null=True
        )

    producto_variante = serializers.PrimaryKeyRelatedField(
        queryset=ProductoVariante.objects.all(),
        required=False,
        allow_null=True,
    )
    producto = serializers.PrimaryKeyRelatedField(
        queryset=Producto.objects.all(),
        required=False,
        allow_null=True,
    )
    cantidad = serializers.IntegerField(min_value=1, max_value=10000, required=False, default=1)
    rfid_mode = serializers.BooleanField(required=False, default=True)
    printer_name = serializers.CharField(
        max_length=128, required=False, allow_blank=True, allow_null=True
    )
    printer_address = serializers.CharField(
        max_length=128, required=False, allow_blank=True, allow_null=True
    )
    status = serializers.ChoiceField(
        choices=EtiquetaRFIDImpresion.Estatus.choices, required=False
    )
    zpl_enviado = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    observaciones = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    etiquetas = EtiquetaRFIDDetalleInputSerializer(many=True, required=False)

    def validate(self, attrs):
        producto = attrs.get("producto")
        producto_variante = attrs.get("producto_variante")
        if not producto and not producto_variante:
            raise serializers.ValidationError(
                "Debe proporcionar 'producto' o 'producto_variante'."
            )
        if producto and producto_variante:
            raise serializers.ValidationError(
                "Solo puede proporcionar 'producto' o 'producto_variante'."
            )
        return attrs


