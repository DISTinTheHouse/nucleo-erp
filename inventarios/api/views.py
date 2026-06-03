from django.db import models
from rest_framework import viewsets, permissions
from rest_framework.exceptions import PermissionDenied
from inventarios.models import Almacen, Ubicacion, Existencia, MovimientoInventario, MovimientoInventarioDetalle, AjusteInventario
from .serializers import AlmacenSerializer, UbicacionSerializer, ExistenciaSerializer, MovimientoInventarioSerializer, MovimientoInventarioDetalleSerializer, AjusteInventarioSerializer

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
    queryset = Ubicacion.objects.all().select_related('almacen__empresa', 'almacen__sucursal')
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
            models.Q(almacen__empresa_id__in=empresa_ids) &
            models.Q(almacen__sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        # Obtener el almacén del validated_data
        almacen = serializer.validated_data.get('almacen')
        
        if not user.is_superuser and almacen:
            sucursal = almacen.sucursal
            empresa = almacen.empresa
            
            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if user.empresa_id and empresa.pk != user.empresa_id and not user.empresas.filter(pk=empresa.pk).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()

class ExistenciaViewSet(viewsets.ModelViewSet):
    queryset = Existencia.objects.all().select_related(
        "producto_variante__producto",
        "producto_variante__color",
        "producto_variante__talla",
        "almacen",
        "ubicacion",
    )
    serializer_class = ExistenciaSerializer
    permission_classes = [IsAuthenticatedAndScoped]

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
        
        # Filtrar por almacenes permitidos (ya que Existencia depende de Almacen)
        return qs.filter(
            models.Q(almacen__empresa_id__in=empresa_ids) &
            models.Q(almacen__sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        instance = serializer.save()
        if not user.is_superuser:
            # Validar acceso al almacén asociado
            almacen = instance.almacen
            if almacen:
                if almacen.sucursal_id and not user.sucursales.filter(pk=almacen.sucursal_id).exists():
                    raise PermissionDenied("No tiene acceso a la sucursal de este almacén")
                if almacen.empresa_id:
                    if user.empresa_id and almacen.empresa_id != user.empresa_id and not user.empresas.filter(pk=almacen.empresa_id).exists():
                        raise PermissionDenied("No tiene acceso a la empresa de este almacén")

class MovimientoInventarioViewSet(viewsets.ModelViewSet):
    queryset = MovimientoInventario.objects.all().select_related('empresa', 'sucursal', 'pedido', 'entrega', 'devolucion', 'ajuste_inventario')
    serializer_class = MovimientoInventarioSerializer
    permission_classes = [IsAuthenticatedAndScoped]

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
        if not user.is_superuser:
            sucursal = serializer.validated_data.get('sucursal')
            empresa = serializer.validated_data.get('empresa')

            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if user.empresa_id and empresa.pk != user.empresa_id and not user.empresas.filter(pk=empresa.pk).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()

class MovimientoInventarioDetalleViewSet(viewsets.ModelViewSet):
    queryset = MovimientoInventarioDetalle.objects.all().select_related('movimiento_inventario', 'producto', 'ubicacion_origen', 'ubicacion_destino', 'lote', 'serie')
    serializer_class = MovimientoInventarioDetalleSerializer
    permission_classes = [IsAuthenticatedAndScoped]

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
            models.Q(movimiento_inventario__empresa_id__in=empresa_ids) &
            models.Q(movimiento_inventario__sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_superuser:
            movimiento = serializer.validated_data.get('movimiento_inventario')
            if movimiento:
                if movimiento.sucursal_id and not user.sucursales.filter(pk=movimiento.sucursal_id).exists():
                    raise PermissionDenied("No tiene acceso a la sucursal del movimiento de inventario")
                if movimiento.empresa_id:
                    if user.empresa_id and movimiento.empresa_id != user.empresa_id and not user.empresas.filter(pk=movimiento.empresa_id).exists():
                        raise PermissionDenied("No tiene acceso a la empresa del movimiento de inventario")
        serializer.save()

class AjusteInventarioViewSet(viewsets.ModelViewSet):
    queryset = AjusteInventario.objects.all().select_related('empresa', 'sucursal', 'almacen')
    serializer_class = AjusteInventarioSerializer
    permission_classes = [IsAuthenticatedAndScoped]

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
        if not user.is_superuser:
            sucursal = serializer.validated_data.get('sucursal')
            empresa = serializer.validated_data.get('empresa')
            
            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if user.empresa_id and empresa.pk != user.empresa_id and not user.empresas.filter(pk=empresa.pk).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()
