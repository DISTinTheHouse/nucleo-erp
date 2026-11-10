from rest_framework import serializers
from nucleo.permisos import permisos_efectivos
from seguridad.models import Rol, UsuarioRol
from ..alcance import errores_de_alcance
from ..models import Usuario

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
        # El cálculo ``(roles activos + grants) - denies`` vive en
        # ``nucleo.permisos``, que es también el que usa el buscador global. Antes
        # estaba copiado aquí y en ``LoginAPIView``, con la misma precedencia
        # escrita tres veces: cambiarla en un sitio y no en otro dejaba al frontend
        # pintando secciones que el backend ya no servía.
        #
        # ``.claves()`` conserva el contrato publicado: lista vacía para
        # superusuario y admin de empresa, a quienes el frontend trata como que lo
        # tienen todo.
        return permisos_efectivos(obj).claves()

    def validate(self, data):
        """
        Validación cruzada de integridad.
        """
        request = self.context.get('request')
        request_user = getattr(request, 'user', None)

        roles_ids = data.get('roles')
        empresa_final = self._empresa_destino_id(data, request_user)

        # Sucursales, departamentos y sucursal por defecto, de la empresa del
        # usuario editado aunque llegue uno solo (EC-844). Antes solo se comparaba
        # sucursal_default cuando venía junto con empresa.
        errores = errores_de_alcance(
            empresa_final,
            sucursal_default=data.get('sucursal_default'),
            sucursales=data.get('sucursales'),
            departamentos=data.get('departamentos'),
        )
        if errores:
            raise serializers.ValidationError(errores)

        if roles_ids is not None:
            if not request_user or not request_user.is_authenticated:
                raise serializers.ValidationError({"roles": "No autenticado."})
            if not (request_user.is_superuser or getattr(request_user, 'is_admin_empresa', False)):
                raise serializers.ValidationError({"roles": "No tienes permisos para asignar roles."})

            if not empresa_final:
                raise serializers.ValidationError({"roles": "No se puede validar roles sin empresa."})

            roles_qs = Rol.objects.filter(id__in=roles_ids, empresa_id=empresa_final)
            if roles_qs.count() != len(set(roles_ids)):
                raise serializers.ValidationError({"roles": "Uno o más roles no existen o no pertenecen a la empresa."})

        return data

    def _empresa_destino_id(self, data, request_user):
        """Id de la empresa en la que queda el usuario editado: la del admin de
        empresa (``UsuarioViewSet`` y el asistente la fuerzan al guardar); para
        el superusuario, la que llega o la que ya tenía. Por id, sin leer la
        fila de ``empresas``."""
        if (
            request_user is not None
            and getattr(request_user, 'is_admin_empresa', False)
            and not getattr(request_user, 'is_superuser', False)
        ):
            return getattr(request_user, 'empresa_id', None)
        # La clave manda aunque traiga ``null``: el usuario queda sin empresa.
        if 'empresa' in data:
            return data['empresa'].pk if data['empresa'] is not None else None
        return getattr(self.instance, 'empresa_id', None)

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


class PerfilUsuarioSerializer(UsuarioSerializer):
    """Perfil propio en ``/api/auth/user/`` (``AUTH_KIT['USER_SERIALIZER']``).

    ``UserView`` de auth_kit edita ``request.user`` sin más permiso que estar
    autenticado, así que aquí solo se escriben nombre, apellido, teléfono y
    avatar (EC-844). La respuesta conserva los campos de ``UsuarioSerializer``
    (NextAuth lee id, username, email, nombre, permisos y banderas). Banderas,
    empresa, alcance, roles, estatus, email y username se ignoran; la contraseña
    se rechaza: solo cambia por ``/api/auth/password/change/``, que pide la actual.
    """

    password = None
    roles = None

    class Meta(UsuarioSerializer.Meta):
        fields = [f for f in UsuarioSerializer.Meta.fields if f not in ("password", "roles")]
        read_only_fields = [
            f for f in fields if f not in ("first_name", "last_name", "telefono", "avatar_url")
        ]
        extra_kwargs = {}

    def validate(self, data):
        if "password" in self.initial_data:
            raise serializers.ValidationError({
                "password": "La contraseña se cambia en /api/auth/password/change/ con la actual."
            })
        return data
