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

    def create(self, validated_data):
        return super().create(validated_data)
