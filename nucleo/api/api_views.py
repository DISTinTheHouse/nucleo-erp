from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status, viewsets, permissions
from django.shortcuts import get_object_or_404
from django.db import models
from ..choices import StatusChoices
from ..models import (
    Empresa, Sucursal, Departamento, Moneda, SerieFolio,
    SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago, 
    SatClaveProdServ, SatClaveUnidad,
    EmpresaSatConfig,
    UnidadMedida, Impuesto
)
from .serializers import (
    SatRegimenFiscalSerializer, 
    SatUsoCfdiSerializer, SatMetodoPagoSerializer, SatFormaPagoSerializer,
    SatClaveProdServSerializer, SatClaveUnidadSerializer,
    UnidadMedidaSerializer, ImpuestoSerializer,
    EmpresaSatConfigSerializer,
    EmpresaSerializer, SucursalSerializer, DepartamentoSerializer, MonedaSerializer, SerieFolioSerializer
)
from seguridad.api.api_views import IsSuperUserOrReadOnly

# --- VIEWSETS (Movidios desde views.py para limpiar arquitectura) ---

class EmpresaViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar empresas.
    Permite crear empresas a usuarios autenticados y las vincula automáticamente.
    """
    queryset = Empresa.objects.all().order_by('-created_at')
    serializer_class = EmpresaSerializer
    lookup_field = 'codigo'
    permission_classes = [IsSuperUserOrReadOnly]

    def perform_create(self, serializer):
        # Al crear una empresa, vincular al usuario creador
        empresa = serializer.save()
        user = self.request.user
        
        # Asignar usuario a la empresa y hacerlo admin
        if not user.empresa:
            user.empresa = empresa
            user.is_admin_empresa = True
            user.save()
            
        # Añadir a la lista de empresas permitidas (M2M)
        user.empresas.add(empresa)
    
    def perform_destroy(self, instance):
        instance.soft_delete()

class SucursalViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar sucursales.
    """
    queryset = Sucursal.objects.all()
    serializer_class = SucursalSerializer
    lookup_field = 'codigo'
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        if hasattr(user, 'empresa') and user.empresa:
            # Filtrar por empresa del usuario
            qs = self.queryset.filter(empresa=user.empresa)
            # Y filtrar por sucursales asignadas al usuario (si no es admin empresa)
            if not getattr(user, 'is_admin_empresa', False):
                 qs = qs.filter(id_sucursal__in=user.sucursales.values_list('id_sucursal', flat=True))
            return qs
        return self.queryset.none()
    
    def perform_destroy(self, instance): 
        instance.soft_delete()

class DepartamentoViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar departamentos.
    """
    queryset = Departamento.objects.all()
    serializer_class = DepartamentoSerializer
    lookup_field = 'codigo'
    permission_classes = [IsSuperUserOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        if hasattr(user, 'empresa') and user.empresa:
            return self.queryset.filter(empresa=user.empresa)
        return self.queryset.none()
    
    def perform_destroy(self, instance):
        instance.soft_delete()

class MonedaViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar monedas.
    Admite monedas Globales (System) y Privadas (Empresa).
    """
    queryset = Moneda.objects.all()
    serializer_class = MonedaSerializer
    lookup_field = 'codigo_iso'
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # 1. Monedas globales (empresa=None)
        q_global = models.Q(empresa__isnull=True)

        if user.is_superuser:
            empresa_id = self.request.query_params.get("empresa_id") or self.request.query_params.get("empresa")
            if empresa_id:
                return self.queryset.filter(q_global | models.Q(empresa_id=empresa_id))
            return self.queryset

        # 2. Monedas de la empresa del usuario
        if user.empresa:
            q_empresa = models.Q(empresa=user.empresa)
            return self.queryset.filter(q_global | q_empresa)

        return self.queryset.filter(q_global)

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_superuser:
            # Forzar empresa del usuario si no es superuser
            serializer.save(empresa=user.empresa)
        else:
            serializer.save()

    def perform_destroy(self, instance):
        instance.soft_delete()


class SerieFolioViewSet(viewsets.ModelViewSet):
    """
    API endpoint para gestionar Series y Folios de documentos.
    Filtrado por empresa del usuario.
    """
    queryset = SerieFolio.objects.all()
    serializer_class = SerieFolioSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        
        if user.empresa:
            # Solo mostrar series de la empresa del usuario
            return self.queryset.filter(empresa=user.empresa)
            
        return self.queryset.none()

    def perform_create(self, serializer):
        user = self.request.user
        # Asegurar que se crea para la empresa del usuario si no es superuser
        if not user.is_superuser and user.empresa:
             serializer.save(empresa=user.empresa)
        else:
             serializer.save()
    
    def perform_destroy(self, instance):
        instance.soft_delete()

class SatClaveProdServViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint para buscar Claves de Producto/Servicio SAT.
    Soporta búsqueda por 'q' (código o descripción).
    """
    queryset = SatClaveProdServ.objects.filter(activo=True)
    serializer_class = SatClaveProdServSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get('q', None)
        if q:
            qs = qs.filter(models.Q(codigo__icontains=q) | models.Q(descripcion__icontains=q))
        return qs

class SatClaveUnidadViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint para buscar Claves de Unidad SAT.
    Soporta búsqueda por 'q' (código o descripción).
    """
    queryset = SatClaveUnidad.objects.filter(activo=True)
    serializer_class = SatClaveUnidadSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get('q', None)
        if q:
            qs = qs.filter(models.Q(codigo__icontains=q) | models.Q(descripcion__icontains=q))
        return qs

class UnidadMedidaViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint para Unidades de Medida (Sistema CORE).
    """
    queryset = UnidadMedida.objects.filter(activo=True)
    serializer_class = UnidadMedidaSerializer
    permission_classes = [permissions.IsAuthenticated]

class ImpuestoViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint para Impuestos (Sistema CORE).
    """
    queryset = Impuesto.objects.filter(activo=True)
    serializer_class = ImpuestoSerializer
    permission_classes = [permissions.IsAuthenticated]

# --- API VIEWS CUSTOM ---

class HealthzAPIView(APIView):
    permission_classes = []

    def get(self, request):
        return Response({"ok": True}, status=200)

class EmpresaSatConfigUpdateView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get_object(self, empresa_id):
        # Verificar permisos: usuario debe pertenecer a la empresa o ser superuser
        # Por ahora simple check de existencia
        empresa = get_object_or_404(Empresa, pk=empresa_id)
        # Crear config si no existe
        config, created = EmpresaSatConfig.objects.get_or_create(empresa=empresa)
        return config

    def get(self, request, empresa_id):
        config = self.get_object(empresa_id)
        serializer = EmpresaSatConfigSerializer(config)
        return Response(serializer.data)

    def patch(self, request, empresa_id):
        config = self.get_object(empresa_id)
        # Usamos partial=True para permitir subir solo archivos o solo password
        serializer = EmpresaSatConfigSerializer(config, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class SatCatalogosAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Retorna todos los catálogos del SAT para uso en frontend (cacheable).
        """
        # Regímenes Fiscales
        regimenes = SatRegimenFiscal.objects.filter(activo=True)
        usos = SatUsoCfdi.objects.filter(activo=True)
        metodos = SatMetodoPago.objects.filter(activo=True)
        formas = SatFormaPago.objects.filter(activo=True)

        data = {
            'regimenes_fiscales': SatRegimenFiscalSerializer(regimenes, many=True).data,
            'usos_cfdi': SatUsoCfdiSerializer(usos, many=True).data,
            'metodos_pago': SatMetodoPagoSerializer(metodos, many=True).data,
            'formas_pago': SatFormaPagoSerializer(formas, many=True).data,
        }
        return Response(data)

class UserEmpresasAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Retorna las empresas a las que el usuario tiene acceso.
        Incluye la empresa activa (FK) y las empresas permitidas (M2M).
        """
        user = request.user
        if user.is_superuser:
            empresas = Empresa.objects.all()
        else:
            # Combinar FK y M2M
            empresas = Empresa.objects.filter(
                models.Q(pk=user.empresa_id) | 
                models.Q(pk__in=user.empresas.values('pk'))
            ).distinct()
        
        # Usamos el serializer existente o construimos data simple
        data = []
        for emp in empresas:
            data.append({
                'id': emp.pk, # ID interno (UUID o AutoField)
                'codigo': emp.codigo,
                'razon_social': emp.razon_social,
                'nombre_comercial': emp.nombre_comercial,
                'rfc': emp.rfc,
                'logo': emp.logo_url
            })
        
        return Response(data)

class UserSucursalesAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, empresa_id=None):
        """
        Retorna las sucursales permitidas para el usuario dentro de una empresa.
        Filtra por:
        1. La empresa seleccionada.
        2. Las sucursales asignadas al usuario (M2M).
        """
        user = request.user
        
        # Si no se pasa empresa_id en la URL, intentamos query param o usamos la del usuario
        if not empresa_id:
            empresa_id = request.query_params.get('empresa_id')
            
        if not empresa_id and user.empresa:
            empresa_id = user.empresa.pk

        if not empresa_id:
            return Response({'error': 'Empresa ID es requerido'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            empresa_id = int(empresa_id)
        except (TypeError, ValueError):
            return Response({'error': 'Empresa ID inválido'}, status=status.HTTP_400_BAD_REQUEST)

        if not user.is_superuser:
            empresa_ids = []
            if user.empresa_id:
                empresa_ids.append(user.empresa_id)
            empresa_ids += list(user.empresas.values_list('pk', flat=True))
            if empresa_id not in set(empresa_ids):
                return Response({'error': 'No autorizado'}, status=status.HTTP_403_FORBIDDEN)

        # Filtrar sucursales
        # Si es superuser, todas las de la empresa.
        # Si es usuario normal, intersección de (Sucursales de la Empresa) y (Sucursales Asignadas).
         
        active_values = {StatusChoices.ACTIVE, "activo", "ACTIVO"}
        try:
            active_values.add(Sucursal.Estatus.ACTIVO)
        except Exception:
            pass

        qs = Sucursal.objects.filter(empresa_id=empresa_id, activo=True)
        
        if not user.is_superuser and not getattr(user, 'is_admin_empresa', False):
            allowed_ids = list(user.sucursales.values_list('id_sucursal', flat=True))
            if not allowed_ids and getattr(user, 'sucursal_default_id', None):
                try:
                    if user.sucursal_default and user.sucursal_default.empresa_id == empresa_id:
                        allowed_ids = [user.sucursal_default_id]
                except Exception:
                    allowed_ids = allowed_ids
            if allowed_ids:
                qs = qs.filter(id_sucursal__in=allowed_ids)
            else:
                qs = qs.none()

        data = []
        for suc in qs:
            data.append({
                'id': suc.pk,
                'codigo': suc.codigo,
                'nombre': suc.nombre,
                'direccion': suc.direccion_linea1,
                'telefono': suc.telefono
            })

        return Response(data)
