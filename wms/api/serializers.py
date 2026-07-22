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
    producto_nombre = serializers.CharField(source="producto.nombre", read_only=True)
    producto_variante_nombre = serializers.CharField(source="producto_variante.nombre", read_only=True)

    class Meta:
        model = PickingDetalle
        fields = "__all__"
        read_only_fields = ["picking"]


class PickingSerializer(serializers.ModelSerializer):
    picking_detalle = PickingDetalleSerializer(many=True, read_only=True)
    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True)
    usuario_nombre = serializers.SerializerMethodField()
    operador_nombre = serializers.SerializerMethodField()

    def get_usuario_nombre(self, obj):
        usuario = obj.usuario
        if not usuario:
            return None
        return usuario.get_full_name().strip() or usuario.email

    def get_operador_nombre(self, obj):
        operador = obj.operador
        if not operador:
            return None
        return operador.get_full_name().strip() or operador.email

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