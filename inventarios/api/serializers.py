from rest_framework import serializers
from inventarios.models import Almacen, Ubicacion
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
    class Meta:
        model = Ubicacion
        fields = [
            'id_ubicacion', 'empresa', 'sucursal', 'almacen', 'codigo', 'nombre', 'estatus',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id_ubicacion', 'created_at', 'updated_at', 'empresa', 'sucursal']

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        almacen = attrs.get('almacen') or getattr(self.instance, 'almacen', None)

        if not almacen:
            raise serializers.ValidationError({'almacen': 'Almacén es requerido'})

        # Enforce empresa/sucursal derivados del almacén
        attrs['empresa'] = almacen.empresa
        attrs['sucursal'] = almacen.sucursal

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
        # Recalcular empresa/sucursal si cambió el almacén
        if 'almacen' in validated_data and validated_data['almacen']:
            validated_data['empresa'] = validated_data['almacen'].empresa
            validated_data['sucursal'] = validated_data['almacen'].sucursal
        return super().update(instance, validated_data)
