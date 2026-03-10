from django.db.models import Q
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from ..models import Rol, Permiso
from .serializers import RolSerializer, RolPermisosSerializer, PermisoSerializer

# Custom Permission
class IsSuperUserOrReadOnly(permissions.BasePermission):
    """
    Permite acceso total a superusuarios.
    Lectura permitida a usuarios autenticados (sujeta a filtros de queryset).
    Escritura prohibida para no superusuarios.
    """
    def has_permission(self, request, view):
        # Permitir métodos seguros a autenticados
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
            
        # Para acciones personalizadas de permisos (POST/PUT), permitir si es superusuario o admin de empresa
        if view.action == 'permisos':
            return request.user and request.user.is_authenticated and (request.user.is_superuser or getattr(request.user, 'is_admin_empresa', False))
            
        return request.user and request.user.is_superuser

# API
class RolViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar roles.
    """
    queryset = Rol.objects.all()
    serializer_class = RolSerializer
    permission_classes = [IsSuperUserOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        if hasattr(user, 'empresa') and user.empresa:
            return self.queryset.filter(empresa=user.empresa)
        return self.queryset.none()

    @action(detail=True, methods=['get', 'put'], serializer_class=RolPermisosSerializer)
    def permisos(self, request, pk=None):
        """
        Endpoint para gestionar los permisos de un rol específico.
        GET: Retorna los IDs de permisos asignados.
        PUT: Actualiza la lista de permisos asignados (recibe lista de IDs).
        """
        rol = self.get_object()
        
        if request.method == 'GET':
            # Retornar lista de IDs de permisos asignados
            permisos_ids = rol.permisos.values_list('id', flat=True)
            return Response({'permisos': list(permisos_ids)})
            
        elif request.method == 'PUT':
            serializer = self.get_serializer(rol, data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response({'status': 'Permisos actualizados correctamente', 'permisos': request.data.get('permisos', [])})
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PermisoViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Permiso.objects.all().order_by('modulo', 'clave')
    serializer_class = PermisoSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get('q')
        modulo = self.request.query_params.get('modulo')
        if modulo:
            qs = qs.filter(modulo__iexact=modulo)
        if q:
            qs = qs.filter(
                Q(clave__icontains=q) |
                Q(nombre__icontains=q) |
                Q(descripcion__icontains=q)
            )
        return qs
