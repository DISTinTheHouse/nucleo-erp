from rest_framework import serializers
from .models import Rol, Permiso, RolPermiso

class PermisoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permiso
        fields = '__all__'

class RolSerializer(serializers.ModelSerializer):
    permisos_count = serializers.IntegerField(source='permisos.count', read_only=True)
    
    class Meta:
        model = Rol
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']

class RolPermisosSerializer(serializers.Serializer):
    """
    Serializer para asignar permisos a un rol de forma masiva.
    """
    permisos = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=True,
        help_text="Lista de IDs de permisos a asignar"
    )

    def update(self, instance, validated_data):
        permisos_ids = validated_data.get('permisos', [])
        permisos_qs = Permiso.objects.filter(id__in=permisos_ids)
        instance.permisos.set(permisos_qs)
        return instance
