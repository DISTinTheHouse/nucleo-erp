from rest_framework import serializers
from seguridad.models import Rol, UsuarioRol
from ..models import Usuario
from auth_kit.serializers.login_factors import LoginRequestSerializer as _AuthKitDefaultLoginRequestSerializer

class UsuarioSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    roles = serializers.ListField(child=serializers.IntegerField(), required=False, write_only=True)
    roles_ids = serializers.SerializerMethodField(read_only=True)
    permisos = serializers.SerializerMethodField(read_only=True)
    empresa_id = serializers.SerializerMethodField(read_only=True)
    nombre_completo = serializers.SerializerMethodField(read_only=True)
    es_admin = serializers.SerializerMethodField(read_only=True)
    
    class Meta:
        model = Usuario
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name', 
            'is_active', 'estatus', 'empresa', 'sucursal_default', 
            'sucursales', 'departamentos', 'telefono', 'avatar_url', 
            'is_admin_empresa', 'is_superuser', 'is_staff',
            'empresa_id', 'nombre_completo', 'es_admin', 'permisos',
            'password', 'roles', 'roles_ids', 'date_joined', 'last_login'
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

    def get_empresa_id(self, obj):
        return getattr(obj, "empresa_id", None)

    def get_nombre_completo(self, obj):
        return f"{getattr(obj, 'first_name', '')} {getattr(obj, 'last_name', '')}".strip()

    def get_es_admin(self, obj):
        return bool(getattr(obj, "is_staff", False) or getattr(obj, "is_superuser", False))

    def get_permisos(self, obj):
        if getattr(obj, "is_superuser", False) or getattr(obj, "is_admin_empresa", False):
            return []

        qs_roles = UsuarioRol.objects.filter(
            usuario=obj,
            rol__estatus="activo",
        ).values_list("rol__permisos__clave", flat=True)
        permisos_roles = set(filter(None, qs_roles))

        permisos_grant = set()
        permisos_deny = set()
        overrides = obj.overrides_permisos.select_related("permiso").all()
        for ov in overrides:
            clave = getattr(getattr(ov, "permiso", None), "clave", None)
            if not clave:
                continue
            if ov.tipo == "grant":
                permisos_grant.add(clave)
            elif ov.tipo == "deny":
                permisos_deny.add(clave)

        permisos_efectivos = (permisos_roles | permisos_grant) - permisos_deny
        return sorted(list(permisos_efectivos))

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


class AuthKitLoginRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(write_only=True, required=False)
    username = serializers.CharField(write_only=True, required=False)
    password = serializers.CharField(style={"input_type": "password"}, write_only=True)

    def validate(self, attrs):
        email = (attrs.get("email") or "").strip()
        if not email:
            email = (attrs.get("username") or "").strip()
        if not email:
            raise serializers.ValidationError({"email": "Este campo es requerido."})

        password = attrs.get("password")
        base = _AuthKitDefaultLoginRequestSerializer(
            context=self.context,
            data={"email": email, "password": password},
        )
        base.is_valid(raise_exception=True)
        user = base.context.get("user")
        if user is not None:
            self.context["user"] = user
        return attrs
