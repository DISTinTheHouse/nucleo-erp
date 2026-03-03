from rest_framework import serializers
from inventarios.models import Almacen, Ubicacion, Existencia, MovimientoInventario, MovimientoInventarioDetalle, AjusteInventario
from nucleo.models import Sucursal

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
    
    class Meta:
        model = Ubicacion
        fields = [
            'id_ubicacion', 'almacen', 'tipo_ubicacion', 'estatus',
            'pasillo', 'rack', 'nivel', 'posicion',
            'orden_recorrido', 'bloqueada_entrada', 'bloqueada_salida',
            'permite_mezcla_productos', 'permite_mezcla_lotes',
            'nombre_completo',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id_ubicacion', 'created_at', 'updated_at', 'nombre_completo']

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
    class Meta:
        model = Existencia
        fields = '__all__'

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