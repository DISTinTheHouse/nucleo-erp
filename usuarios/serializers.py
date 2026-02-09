from rest_framework import serializers
from .models import Usuario

class UsuarioSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    
    class Meta:
        model = Usuario
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 
            'is_active', 'estatus', 'empresa', 'sucursal_default', 
            'sucursales', 'departamentos', 'telefono', 'avatar_url', 
            'is_admin_empresa', 'password', 'date_joined', 'last_login'
        ]
        read_only_fields = ['date_joined', 'last_login', 'is_active']
        extra_kwargs = {
            'email': {'required': True},
            'empresa': {'required': True},
            'sucursal_default': {'required': True}
        }

    def validate(self, data):
        """
        Validación cruzada de integridad.
        """
        empresa = data.get('empresa')
        sucursal = data.get('sucursal_default')

        # Si estamos creando (no hay instancia) o si se están actualizando ambos campos
        if empresa and sucursal:
            if sucursal.empresa != empresa:
                raise serializers.ValidationError({
                    "sucursal_default": "La sucursal seleccionada no pertenece a la empresa asignada."
                })
        
        return data

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = super().create(validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user
