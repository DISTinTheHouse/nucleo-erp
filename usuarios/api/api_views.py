from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from rest_framework import permissions, viewsets, status
from rest_framework.permissions import AllowAny
from rest_framework.exceptions import PermissionDenied
from django.contrib.auth import authenticate
from nucleo.permisos import permisos_efectivos
from ..models import Usuario
from .serializers import UsuarioSerializer

# Custom Permission
class IsSuperUserOrReadOnly(permissions.BasePermission):
    """
    Permite acceso total a superusuarios.
    Lectura permitida a usuarios autenticados (sujeta a filtros de queryset).
    Escritura prohibida para no superusuarios.
    """
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
        return request.user and request.user.is_superuser

class LoginAPIView(APIView):
    """
    DESHABILITADO: este endpoint legado saltaba el flujo de MFA y emitía
    tokens de authtoken sin expiración. Se conserva la clase y la ruta
    (usuarios/urls.py) para no romper referencias existentes, pero
    responde 403 antes de tocar cualquier credencial. Usa el flujo
    estándar de auth_kit (/api/auth/) en su lugar.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        return Response(
            {'error': 'Este endpoint ha sido deshabilitado. Usa el flujo de autenticación estándar.'},
            status=status.HTTP_403_FORBIDDEN,
        )

    def _legacy_post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')

        # Limpieza básica
        if email:
            email = email.strip()

        print(f"DEBUG LOGIN: Email recibido: '{email}'")
        # print(f"DEBUG LOGIN: Password recibido: '{password}'") # No imprimir passwords reales en logs

        if not email or not password:
            return Response({'error': 'Email y contraseña son requeridos'}, status=status.HTTP_400_BAD_REQUEST)

        # Autenticar usando el EmailBackend configurado en settings
        user = authenticate(request, email=email, password=password)

        if user:
            if not user.is_active or getattr(user, 'estatus', '') == 'bloqueado':
                return Response({'error': 'Su cuenta está bloqueada o inactiva. Contacte al administrador.'}, status=status.HTTP_403_FORBIDDEN)

            token, created = Token.objects.get_or_create(user=user)

            # Lista de permisos efectivos ``(roles activos + grants) - denies``.
            # El cálculo vive en ``nucleo.permisos``, que es el mismo que usa el
            # buscador global; antes estaba copiado aquí y en
            # ``UsuarioSerializer.get_permisos``, con la misma precedencia escrita
            # tres veces. ``.claves()`` devuelve lista vacía para superusuario y
            # admin de empresa, que es el contrato que ya consumía el frontend.
            is_superuser = user.is_superuser
            is_admin_empresa = getattr(user, 'is_admin_empresa', False)
            permisos_finales = permisos_efectivos(user).claves()

            return Response({
                'token': token.key,
                'user_id': user.pk,
                'email': user.email,
                'username': user.username,
                'nombre_completo': f"{user.first_name} {user.last_name}".strip(),
                'es_admin': user.is_staff or user.is_superuser,
                'is_superuser': is_superuser,
                'is_admin_empresa': is_admin_empresa,
                'empresa_id': user.empresa_id if user.empresa else None,
                'permisos': permisos_finales,
            })
        else:
            return Response({'error': 'Credenciales inválidas'}, status=status.HTTP_401_UNAUTHORIZED)

            
class UsuarioViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar usuarios.
    """
    queryset = Usuario.objects.all()
    serializer_class = UsuarioSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # Listado más reciente primero; ``-id`` como desempate estable.
        base_qs = self.queryset.order_by('-created_at', '-id')
        if user.is_superuser:
            return base_qs
        if hasattr(user, 'empresa') and user.empresa:
            return base_qs.filter(empresa=user.empresa)
        return base_qs.none()

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        
        # Superusuario: acceso total
        if request.user.is_superuser:
            return

        # Admin de Empresa: gestión limitada
        if getattr(request.user, 'is_admin_empresa', False):
            # Solo puede gestionar usuarios de su misma empresa
            if obj.empresa != request.user.empresa:
                raise PermissionDenied("No puedes gestionar usuarios de otra empresa.")
            
            # No puede editar/borrar superusuarios
            if obj.is_superuser:
                raise PermissionDenied("No puedes modificar a un superusuario.")
            
            return

        # Otros usuarios: solo lectura de su propio perfil o según permisos (aquí restringimos escritura)
        if request.method not in permissions.SAFE_METHODS:
             raise PermissionDenied("No tienes permisos para realizar esta acción.")

    def perform_create(self, serializer):
        user = self.request.user
        
        # Caso Superusuario
        if user.is_superuser:
            serializer.save()
            return

        # Caso Admin de Empresa
        if getattr(user, 'is_admin_empresa', False):
            # Validaciones de seguridad
            if serializer.validated_data.get('is_superuser', False):
                raise PermissionDenied("No puedes crear superusuarios.")
            
            if serializer.validated_data.get('is_admin_empresa', False):
                # Opcional: impedir crear otros admins o permitirlo con cuidado. 
                # Por seguridad default: bloqueado.
                raise PermissionDenied("No puedes crear otros administradores de empresa.")

            # Validar integridad de sucursal
            sucursal = serializer.validated_data.get('sucursal_default')
            if sucursal and sucursal.empresa != user.empresa:
                raise PermissionDenied("La sucursal seleccionada no pertenece a tu empresa.")

            # Forzar asignación a la empresa del admin
            serializer.save(empresa=user.empresa)
        else:
            raise PermissionDenied("No tienes permisos para crear usuarios.")

    def perform_update(self, serializer):
        user = self.request.user
        
        # Caso Superusuario
        if user.is_superuser:
            serializer.save()
            return

        # Caso Admin de Empresa
        if getattr(user, 'is_admin_empresa', False):
            # Validaciones de seguridad
            if serializer.validated_data.get('is_superuser', False):
                raise PermissionDenied("No puedes promover a superusuario.")
            
            # Impedir cambiar la empresa del usuario
            if 'empresa' in serializer.validated_data and serializer.validated_data['empresa'] != user.empresa:
                 raise PermissionDenied("No puedes mover usuarios a otra empresa.")

            # Validar integridad de sucursal si se está actualizando
            sucursal = serializer.validated_data.get('sucursal_default')
            if sucursal and sucursal.empresa != user.empresa:
                raise PermissionDenied("La sucursal seleccionada no pertenece a tu empresa.")

            serializer.save(empresa=user.empresa)
        else:
            raise PermissionDenied("No tienes permisos para editar usuarios.")

    def perform_destroy(self, instance):
        user = self.request.user
        if not user.is_superuser:
            if not getattr(user, 'is_admin_empresa', False):
                 raise PermissionDenied("No tienes permisos para eliminar usuarios.")
            if instance.is_superuser:
                 raise PermissionDenied("No puedes eliminar a un superusuario.")
            if instance.empresa != user.empresa:
                 raise PermissionDenied("No puedes eliminar usuarios de otra empresa.")
        
        super().perform_destroy(instance)
