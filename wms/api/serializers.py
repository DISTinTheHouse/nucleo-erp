from rest_framework import serializers
from wms.models import Transferencia, TransferenciaDetalle, Picking, PickingDetalle

class TransferenciaDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransferenciaDetalle
        fields = "__all__"
        read_only_fields = ["transferencia"]
    
    def validate(self, attrs):
        producto = attrs.get('producto')
        producto_variante = attrs.get('producto_variante')

        if not producto and not producto_variante:
            raise serializers.ValidationError({
                "non_field_errors": [
                    "Debe proporcionar 'producto' o 'producto_variante'."
                ]
            })
        
        if producto and producto_variante:
            raise serializers.ValidationError({
                "non_field_errors": [
                    "Solo puede proporcionar 'producto' o 'producto_variante'."
                ]
            })
        
        return attrs

class TransferenciaSerializer(serializers.ModelSerializer):
    transferencia_detalle = TransferenciaDetalleSerializer(many=True)

    class Meta:
        model = Transferencia
        fields = "__all__"
        read_only_fields = ["empresa", "sucursal", "folio", "usuario", "status"]

    def validate(self, attrs):
        almacen_origen = attrs.get('almacen_origen')
        almacen_destino = attrs.get('almacen_destino')

        if almacen_origen == almacen_destino:
            raise serializers.ValidationError({
                "non_field_errors": [
                    "El almacen de origen y destino no pueden ser iguales."
                ]
            })

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

    transferencia_detalle = TransferenciaDetalleReadSerializer(many=True, read_only=True)

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
    """

    producto_nombre = serializers.CharField(
        source="producto.nombre", read_only=True, default=None
    )
    producto_variante_nombre = serializers.CharField(
        source="producto_variante.nombre", read_only=True, default=None
    )
    ubicacion_nombre = serializers.SerializerMethodField()
    operador_nombre = serializers.SerializerMethodField()

    class Meta:
        model = PickingDetalle
        fields = "__all__"
        read_only_fields = ["picking"]

    def get_ubicacion_nombre(self, obj):
        return str(obj.ubicacion) if obj.ubicacion_id else None

    def get_operador_nombre(self, obj):
        operador = obj.operador
        if not operador:
            return None
        # Mismo fallback que ``TransferenciaListSerializer.usuario_nombre``:
        # nombre completo y, si el usuario no tiene first/last name, el email.
        return operador.get_full_name().strip() or operador.email


class PickingSerializer(serializers.ModelSerializer):
    """Serializer compartido de picking (``list``, ``retrieve`` y respuesta del
    ``create``).

    Además de las FK crudas expone los nombres resueltos —misma convención que
    ``TransferenciaListSerializer``—. Todos los ``*_nombre`` son de solo lectura,
    así que el contrato de escritura del ``POST /pickings/`` no cambia.

    ``pedido``/``operador``/``almacen``/``usuario`` son NOT NULL en el modelo
    (``pedido_folio`` aun así puede ser ``null``: ``Pedido.folio`` es nullable).
    ``oleada``/``zona_almacen``/``lote`` son FK opcionales y resuelven a ``null``
    cuando faltan. ``Oleada`` y ``LotePicking`` no tienen campo ``nombre``: su
    etiqueta se compone en ``__str__`` (id + estado), por eso se resuelven con
    ``str()`` null-safe, igual que ``Ubicacion`` en transferencias.
    """

    picking_detalle = PickingDetalleSerializer(many=True, read_only=True)

    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True)
    operador_nombre = serializers.SerializerMethodField()
    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True)
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