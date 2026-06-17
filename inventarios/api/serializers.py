from rest_framework import serializers
from inventarios.models import Almacen, Ubicacion, Existencia, MovimientoInventario, MovimientoInventarioDetalle, AjusteInventario
from nucleo.models import Sucursal
from auditoria.models import AuditoriaEvento

class AlmacenSerializer(serializers.ModelSerializer):
    class Meta:
        model = Almacen
        fields = [
            'id_almacen', 'empresa', 'sucursal', 'codigo', 'nombre', 'estatus',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id_almacen', 'created_at', 'updated_at', 'empresa']

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        sucursal = attrs.get('sucursal') or getattr(self.instance, 'sucursal', None)

        if sucursal and not isinstance(sucursal, Sucursal):
            raise serializers.ValidationError({'sucursal': 'Sucursal inválida'})

        # Enforce empresa derivada de sucursal si existe
        if sucursal:
            attrs['empresa'] = sucursal.empresa

        # Si no es superuser, validar alcance
        if user and not user.is_superuser and sucursal:
            # Usuario debe tener acceso a la sucursal
            if not user.sucursales.filter(pk=sucursal.pk).exists():
                raise serializers.ValidationError({'sucursal': 'No tiene acceso a esta sucursal'})
            # Usuario debe tener acceso a la empresa
            if user.empresa_id and sucursal.empresa_id != user.empresa_id and not user.empresas.filter(pk=sucursal.empresa_id).exists():
                raise serializers.ValidationError({'empresa': 'No tiene acceso a esta empresa'})

        return attrs

    def create(self, validated_data):
        # empresa viene derivada de sucursal en validate()
        return super().create(validated_data)

    def update(self, instance, validated_data):
        # empresa se mantiene consistente con sucursal si cambia
        if 'sucursal' in validated_data and validated_data['sucursal']:
            validated_data['empresa'] = validated_data['sucursal'].empresa
        return super().update(instance, validated_data)

class UbicacionSerializer(serializers.ModelSerializer):
    nombre_completo = serializers.CharField(source='__str__', read_only=True)
    empresa = serializers.IntegerField(source='almacen.empresa_id', read_only=True)
    sucursal = serializers.IntegerField(source='almacen.sucursal_id', read_only=True)
    
    class Meta:
        model = Ubicacion
        fields = [
            'id_ubicacion', 'empresa', 'sucursal', 'almacen', 'tipo_ubicacion', 'estatus',
            'pasillo', 'rack', 'nivel', 'posicion',
            'orden_recorrido', 'bloqueada_entrada', 'bloqueada_salida',
            'permite_mezcla_productos', 'permite_mezcla_lotes',
            'nombre_completo',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id_ubicacion', 'created_at', 'updated_at', 'nombre_completo', 'empresa', 'sucursal']

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        almacen = attrs.get('almacen') or getattr(self.instance, 'almacen', None)

        if not almacen:
            raise serializers.ValidationError({'almacen': 'Almacén es requerido'})

        # Alcance del usuario si no es superuser
        if user and not user.is_superuser:
            # Usuario debe tener acceso a la sucursal del almacén
            if almacen.sucursal and not user.sucursales.filter(pk=almacen.sucursal_id).exists():
                raise serializers.ValidationError({'almacen': 'No tiene acceso al almacén (sucursal no permitida)'})
            # Usuario debe tener acceso a la empresa del almacén
            if user.empresa_id and almacen.empresa_id != user.empresa_id and not user.empresas.filter(pk=almacen.empresa_id).exists():
                raise serializers.ValidationError({'almacen': 'No tiene acceso al almacén (empresa no permitida)'})

        return attrs

    def create(self, validated_data):
        return super().create(validated_data)

    def update(self, instance, validated_data):
        return super().update(instance, validated_data)

class ExistenciaSerializer(serializers.ModelSerializer):
    producto_info = serializers.SerializerMethodField(read_only=True)
    almacen_info = serializers.SerializerMethodField(read_only=True)
    ubicacion_info = serializers.SerializerMethodField(read_only=True)
    lote_info = serializers.SerializerMethodField(read_only=True)
    serie_info = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Existencia
        fields = '__all__'

    def get_producto_info(self, obj):
        variante = getattr(obj, "producto_variante", None)
        producto = getattr(obj, "producto", None) or (getattr(variante, "producto", None) if variante else None)
        if not producto:
            return None
        tipo_producto = getattr(producto, 'tipo', None)
        return {
            'id': producto.pk,
            'nombre': getattr(producto, 'nombre', None),
            'descripcion': getattr(producto, 'descripcion', None),
            'tipo': getattr(tipo_producto, 'codigo', None),
            'tipo_id': getattr(producto, 'tipo_id', None),
            'categoria_producto': getattr(producto, 'categoria_producto_id', None),
            'unidad_medida': getattr(producto, 'unidad_medida_id', None),
            'sku': getattr(variante, 'sku', None) if variante else None,
            'color_id': getattr(variante, 'color_id', None) if variante else None,
            'color': getattr(getattr(variante, 'color', None), 'nombre', None) if variante else None,
            'talla_id': getattr(variante, 'talla_id', None) if variante else None,
            'talla': getattr(getattr(variante, 'talla', None), 'nombre', None) if variante else None,
        }

    def get_almacen_info(self, obj):
        almacen = getattr(obj, 'almacen', None)
        if not almacen:
            return None
        return {
            'id_almacen': getattr(almacen, 'id_almacen', None),
            'codigo': getattr(almacen, 'codigo', None),
            'nombre': getattr(almacen, 'nombre', None),
            'empresa': getattr(almacen, 'empresa_id', None),
            'sucursal': getattr(almacen, 'sucursal_id', None),
            'estatus': getattr(almacen, 'estatus', None),
            'tipo_almacen': getattr(almacen, 'tipo_almacen', None),
        }

    def get_ubicacion_info(self, obj):
        ubicacion = getattr(obj, 'ubicacion', None)
        if not ubicacion:
            return None
        return {
            'id_ubicacion': getattr(ubicacion, 'id_ubicacion', None),
            'almacen': getattr(ubicacion, 'almacen_id', None),
            'tipo_ubicacion': getattr(ubicacion, 'tipo_ubicacion', None),
            'estatus': getattr(ubicacion, 'estatus', None),
            'pasillo': getattr(ubicacion, 'pasillo', None),
            'rack': getattr(ubicacion, 'rack', None),
            'nivel': getattr(ubicacion, 'nivel', None),
            'posicion': getattr(ubicacion, 'posicion', None),
            'nombre_completo': str(ubicacion),
        }

    def get_lote_info(self, obj):
        lote = getattr(obj, 'lote', None)
        if not lote:
            return None
        return {
            'id': getattr(lote, 'id', None),
            'producto': getattr(lote, 'producto_id', None),
        }

    def get_serie_info(self, obj):
        serie = getattr(obj, 'serie', None)
        if not serie:
            return None
        return {
            'id': getattr(serie, 'id', None),
            'producto': getattr(serie, 'producto_id', None),
        }

class MovimientoInventarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = MovimientoInventario
        fields = '__all__'

class MovimientoInventarioDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = MovimientoInventarioDetalle
        fields = '__all__'

class AjusteInventarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = AjusteInventario
        fields = '__all__'


class AuditoriaMovimientoSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="id_evento", read_only=True)
    tipo_movimiento = serializers.CharField(source="accion", read_only=True)
    fecha = serializers.DateTimeField(source="created_at", read_only=True)
    fecha_movimiento = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = AuditoriaEvento
        fields = [
            "id",
            "id_evento",
            "empresa",
            "usuario",
            "modulo",
            "accion",
            "tipo_movimiento",
            "tabla",
            "id_registro",
            "antes_json",
            "despues_json",
            "ip",
            "user_agent",
            "fecha",
            "fecha_movimiento",
            "created_at",
        ]
