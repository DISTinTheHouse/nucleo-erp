from rest_framework import serializers
from seguridad.models import Rol, UsuarioRol
from ..models import Usuario

class UsuarioSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    roles = serializers.ListField(child=serializers.IntegerField(), required=False, write_only=True)
    roles_ids = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = Usuario
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 
            'is_active', 'estatus', 'empresa', 'sucursal_default', 
            'sucursales', 'departamentos', 'telefono', 'avatar_url', 
            'is_admin_empresa', 'password', 'roles', 'roles_ids', 'date_joined', 'last_login'
        ]
        read_only_fields = ['date_joined', 'last_login', 'is_active']
        extra_kwargs = {
            'email': {'required': True},
            'empresa': {'required': True},
            'sucursal_default': {'required': True}
        }

    def get_roles_ids(self, obj):
        return list(
            UsuarioRol.objects.filter(usuario=obj).values_list('rol_id', flat=True)
        )

    def validate(self, data):
        """
        Validación cruzada de integridad.
        """
        request = self.context.get('request')
        request_user = getattr(request, 'user', None)

        empresa = data.get('empresa')
        sucursal = data.get('sucursal_default')
        roles_ids = data.get('roles')

        # Si estamos creando (no hay instancia) o si se están actualizando ambos campos
        if empresa and sucursal:
            if sucursal.empresa != empresa:
                raise serializers.ValidationError({
                    "sucursal_default": "La sucursal seleccionada no pertenece a la empresa asignada."
                })

        if roles_ids is not None:
            if not request_user or not request_user.is_authenticated:
                raise serializers.ValidationError({"roles": "No autenticado."})
            if not (request_user.is_superuser or getattr(request_user, 'is_admin_empresa', False)):
                raise serializers.ValidationError({"roles": "No tienes permisos para asignar roles."})

            empresa_final = None
            if getattr(request_user, 'is_admin_empresa', False) and not request_user.is_superuser:
                empresa_final = getattr(request_user, 'empresa', None)
            else:
                empresa_final = data.get('empresa') or getattr(self.instance, 'empresa', None)

            if not empresa_final:
                raise serializers.ValidationError({"roles": "No se puede validar roles sin empresa."})

            roles_qs = Rol.objects.filter(id__in=roles_ids, empresa=empresa_final)
            if roles_qs.count() != len(set(roles_ids)):
                raise serializers.ValidationError({"roles": "Uno o más roles no existen o no pertenecen a la empresa."})
        
        return data

    def _set_roles(self, user, roles_ids):
        UsuarioRol.objects.filter(usuario=user).delete()
        if not roles_ids:
            return
        roles_qs = Rol.objects.filter(id__in=roles_ids, empresa=user.empresa)
        UsuarioRol.objects.bulk_create(
            [UsuarioRol(usuario=user, rol=rol, empresa=user.empresa) for rol in roles_qs]
        )

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        roles_ids = validated_data.pop('roles', None)
        user = super().create(validated_data)
        if password:
            user.set_password(password)
            user.save()
        if roles_ids is not None:
            self._set_roles(user, roles_ids)
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        roles_ids = validated_data.pop('roles', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save()
        if roles_ids is not None:
            self._set_roles(user, roles_ids)
        return user
