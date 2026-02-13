from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db import models
from .models import Empresa, Sucursal, SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago, EmpresaSatConfig
from .serializers import (
    SatRegimenFiscalSerializer, 
    SatUsoCfdiSerializer, SatMetodoPagoSerializer, SatFormaPagoSerializer,
    EmpresaSatConfigSerializer
)

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
        regimenes = SatRegimenFiscal.objects.filter(estatus='activo')
        usos = SatUsoCfdi.objects.filter(estatus='activo')
        metodos = SatMetodoPago.objects.filter(estatus='activo')
        formas = SatFormaPago.objects.filter(estatus='activo')

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

        # Filtrar sucursales
        # Si es superuser, todas las de la empresa.
        # Si es usuario normal, intersección de (Sucursales de la Empresa) y (Sucursales Asignadas).
        
        qs = Sucursal.objects.filter(empresa_id=empresa_id, estatus=Sucursal.Estatus.ACTIVO)
        
        if not user.is_superuser:
            # Filtrar solo las que están en user.sucursales
            # Nota: user.sucursales es M2M.
            qs = qs.filter(id_sucursal__in=user.sucursales.values_list('id_sucursal', flat=True))

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
