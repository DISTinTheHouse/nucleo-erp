from django.db import models
from rest_framework import viewsets, permissions
from rest_framework.exceptions import PermissionDenied
from inventarios.models import Almacen, Ubicacion, Existencia, MovimientoInventario
from .serializers import AlmacenSerializer, UbicacionSerializer, ExistenciaSerializer, MovimientoInventarioSerializer

class IsAuthenticatedAndScoped(permissions.BasePermission):
    """
    - Permite lectura a autenticados.
    - Crea/edita solo si es admin de empresa o superuser.
    - Elimina solo si es admin de empresa o superuser.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        # Write operations: require admin_empresa or superuser
        return request.user.is_superuser or getattr(request.user, 'is_admin_empresa', False)

    def has_object_permission(self, request, view, obj):
        # Lectura ya pasó por has_permission
        if request.method in permissions.SAFE_METHODS:
            return True
        # Para write/delete, mismo criterio: admin_empresa o superuser
        return request.user.is_superuser or getattr(request.user, 'is_admin_empresa', False)

class AlmacenViewSet(viewsets.ModelViewSet):
    queryset = Almacen.objects.all().select_related('empresa', 'sucursal')
    serializer_class = AlmacenSerializer
    permission_classes = [IsAuthenticatedAndScoped]
    lookup_field = 'id_almacen'

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        # Filtrar por empresas y sucursales permitidas
        empresa_ids = []
        if user.empresa_id:
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list('pk', flat=True))
        sucursal_ids = list(user.sucursales.values_list('pk', flat=True))
        return qs.filter(
            models.Q(empresa_id__in=empresa_ids) &
            models.Q(sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        instance = serializer.save()
        # Verificación final de alcance: empresa/sucursal dentro del scope del usuario
        if not user.is_superuser:
            if instance.sucursal_id and not user.sucursales.filter(pk=instance.sucursal_id).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            if instance.empresa_id:
                if user.empresa_id and instance.empresa_id != user.empresa_id and not user.empresas.filter(pk=instance.empresa_id).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")

class UbicacionViewSet(viewsets.ModelViewSet):
    queryset = Ubicacion.objects.all().select_related('empresa', 'sucursal', 'almacen')
    serializer_class = UbicacionSerializer
    permission_classes = [IsAuthenticatedAndScoped]
    lookup_field = 'id_ubicacion'

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        empresa_ids = []
        if user.empresa_id:
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list('pk', flat=True))
        sucursal_ids = list(user.sucursales.values_list('pk', flat=True))
        return qs.filter(
            models.Q(empresa_id__in=empresa_ids) &
            models.Q(sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        instance = serializer.save()
        if not user.is_superuser:
            if instance.sucursal_id and not user.sucursales.filter(pk=instance.sucursal_id).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            if instance.empresa_id:
                if user.empresa_id and instance.empresa_id != user.empresa_id and not user.empresas.filter(pk=instance.empresa_id).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")

class ExistenciaViewSet(viewsets.ModelViewSet):
    queryset = Existencia.objects.all()
    serializer_class = ExistenciaSerializer

class MovimientoInventarioViewSet(viewsets.ModelViewSet):
    queryset = MovimientoInventario.objects.all()
    serializer_class = MovimientoInventarioSerializer
