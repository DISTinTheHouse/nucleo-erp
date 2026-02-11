from django.shortcuts import render, get_object_or_404
from django.urls import reverse_lazy
from django.http import JsonResponse
from django.views.generic import TemplateView, ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from rest_framework import viewsets

from django.contrib.auth import get_user_model
from .models import (
    Empresa, Sucursal, Departamento, Moneda, Impuesto, UnidadMedida,
    SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago, SatClaveProdServ, SatClaveUnidad
)
from usuarios.models import Usuario
from .serializers import EmpresaSerializer, SucursalSerializer, DepartamentoSerializer, MonedaSerializer
from .forms import EmpresaForm, SucursalForm, DepartamentoForm, MonedaForm, ImpuestoForm, UnidadMedidaForm
from .mixins import AuditLogMixin
from rest_framework import viewsets, permissions

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

# API
class EmpresaViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar empresas.
    Permite crear empresas a usuarios autenticados y las vincula automáticamente.
    """
    queryset = Empresa.objects.all().order_by('-created_at')
    serializer_class = EmpresaSerializer
    lookup_field = 'codigo'
    permission_classes = [IsSuperUserOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        
        # Filtrar por empresas asignadas al usuario (M2M) o la empresa activa (FK)
        from django.db import models
        queryset = self.queryset.filter(
            models.Q(pk=user.empresa_id) | 
            models.Q(pk__in=user.empresas.values('pk'))
        ).distinct()
        
        return queryset

    def get_object(self):
        """
        Permite buscar por ID (pk) o por el campo lookup_field (código).
        """
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup_value = self.kwargs.get(lookup_url_kwarg)

        # Si el valor es numérico, intentamos buscar por ID (pk)
        if lookup_value and lookup_value.isdigit():
             try:
                 obj = queryset.get(pk=lookup_value)
                 self.check_object_permissions(self.request, obj)
                 return obj
             except queryset.model.DoesNotExist:
                 pass # Fallback a búsqueda por código (por si el código es numérico)
        
        # Búsqueda normal por el campo configurado (código)
        filter_kwargs = {self.lookup_field: lookup_value}
        obj = get_object_or_404(queryset, **filter_kwargs)
        self.check_object_permissions(self.request, obj)
        return obj

    def perform_create(self, serializer):
        empresa = serializer.save()
        user = self.request.user
        
        # 1. Vincular al creador con la empresa (M2M)
        user.empresas.add(empresa)
        
        # 2. Si no tiene empresa activa, asignarla como default y hacerlo admin
        if not user.empresa:
            user.empresa = empresa
            user.is_admin_empresa = True
            user.save()

class SucursalViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar sucursales.
    """
    queryset = Sucursal.objects.all()
    serializer_class = SucursalSerializer
    lookup_field = 'codigo'
    permission_classes = [IsSuperUserOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        if hasattr(user, 'empresa') and user.empresa:
            return self.queryset.filter(empresa=user.empresa)
        return self.queryset.none()

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

class MonedaViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar monedas.
    Admite monedas Globales (System) y Privadas (Empresa).
    """
    queryset = Moneda.objects.all()
    serializer_class = MonedaSerializer
    lookup_field = 'codigo_iso'
    permission_classes = [permissions.IsAuthenticated] # Permitimos a usuarios autenticados, filtramos lógica dentro

    def get_queryset(self):
        user = self.request.user
        # Superusuario ve todo
        if user.is_superuser:
            return self.queryset
            
        # Usuarios normales ven: Globales + Suyas
        from django.db.models import Q
        qs = self.queryset.filter(
            Q(empresa__isnull=True) |  # Globales
            Q(empresa=user.empresa)    # Propias
        )
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        if user.is_superuser:
            # Superusuario crea globales por defecto (empresa=None)
            serializer.save(empresa=None)
        else:
            # Validar que sea Admin de Empresa
            if not getattr(user, 'is_admin_empresa', False):
                raise permissions.exceptions.PermissionDenied("Solo los administradores de empresa pueden agregar monedas.")

            # Admin de empresa crea privadas
            if not user.empresa:
                raise permissions.exceptions.PermissionDenied("Usuario sin empresa asignada.")
            serializer.save(empresa=user.empresa)

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        
        # Si es solo lectura, permitir
        if request.method in permissions.SAFE_METHODS:
            return

        # Bloquear edición/borrado de Globales para no-superusers
        if obj.empresa is None:
            if not request.user.is_superuser:
                raise permissions.exceptions.PermissionDenied("No puedes editar monedas del catálogo global.")
            return

        # Bloquear edición/borrado de Privadas para no-admins
        if not getattr(request.user, 'is_admin_empresa', False):
             raise permissions.exceptions.PermissionDenied("Solo los administradores pueden gestionar monedas de la empresa.")

def get_sucursales_por_empresa(request, empresa_id):
    """
    Retorna JSON con las sucursales activas de una empresa específica,
    incluyendo sus departamentos activos y roles disponibles.
    """
    # 1. Sucursales y Departamentos
    sucursales = Sucursal.objects.filter(empresa_id=empresa_id, estatus=Sucursal.Estatus.ACTIVO).order_by('codigo')
    data_sucursales = []
    for s in sucursales:
        depts = s.departamentos.filter(estatus=Departamento.Estatus.ACTIVO).values('id_departamento', 'nombre', 'codigo')
        data_sucursales.append({
            'id_sucursal': s.id_sucursal,
            'nombre': s.nombre,
            'codigo': s.codigo,
            'departamentos': list(depts)
        })
    
    # 2. Roles disponibles
    from seguridad.models import Rol
    roles = Rol.objects.filter(empresa_id=empresa_id, estatus=Rol.Estatus.ACTIVO).values('id', 'nombre', 'clave_departamento')

    return JsonResponse({
        'sucursales': data_sucursales,
        'roles': list(roles)
    }, safe=False)

# WEB CORE
class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

class CoreDashboardView(LoginRequiredMixin, SuperuserRequiredMixin, TemplateView):
    template_name = 'nucleo/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['total_empresas'] = Empresa.objects.count()
        context['total_sucursales'] = Sucursal.objects.count()
        context['total_usuarios'] = Usuario.objects.count()
        return context

# --- EMPRESA ---
class EmpresaListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Empresa
    template_name = 'nucleo/empresa_list.html'
    context_object_name = 'empresas'
    ordering = ['-created_at']

    def get_queryset(self):
        # Garantizar que se retornen todas las empresas para el superusuario
        return Empresa.objects.all().order_by('-created_at')

class EmpresaCreateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Empresa
    form_class = EmpresaForm
    template_name = 'nucleo/empresa_form.html'
    success_url = reverse_lazy('nucleo:empresa_list')

class EmpresaUpdateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Empresa
    form_class = EmpresaForm
    template_name = 'nucleo/empresa_form.html'
    success_url = reverse_lazy('nucleo:empresa_list')

class EmpresaDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Empresa
    template_name = 'nucleo/empresa_confirm_delete.html'
    success_url = reverse_lazy('nucleo:empresa_list')

# --- SUCURSAL ---
class SucursalListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Sucursal
    template_name = 'nucleo/sucursal_list.html'
    context_object_name = 'sucursales'

class SucursalCreateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Sucursal
    form_class = SucursalForm
    template_name = 'nucleo/sucursal_form.html'
    success_url = reverse_lazy('nucleo:sucursal_list')

class SucursalUpdateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Sucursal
    form_class = SucursalForm
    template_name = 'nucleo/sucursal_form.html'
    success_url = reverse_lazy('nucleo:sucursal_list')

class SucursalDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Sucursal
    template_name = 'nucleo/sucursal_confirm_delete.html'
    success_url = reverse_lazy('nucleo:sucursal_list')

# --- DEPARTAMENTO ---
class DepartamentoListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Departamento
    template_name = 'nucleo/departamento_list.html'
    context_object_name = 'departamentos'

class DepartamentoCreateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Departamento
    form_class = DepartamentoForm
    template_name = 'nucleo/departamento_form.html'
    success_url = reverse_lazy('nucleo:departamento_list')

class DepartamentoUpdateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Departamento
    form_class = DepartamentoForm
    template_name = 'nucleo/departamento_form.html'
    success_url = reverse_lazy('nucleo:departamento_list')

class DepartamentoDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Departamento
    template_name = 'nucleo/departamento_confirm_delete.html'
    success_url = reverse_lazy('nucleo:departamento_list')

# --- MONEDA ---
class MonedaListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Moneda
    template_name = 'nucleo/moneda_list.html'
    context_object_name = 'monedas'

class MonedaCreateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Moneda
    form_class = MonedaForm
    template_name = 'nucleo/moneda_form.html'
    success_url = reverse_lazy('nucleo:moneda_list')

class MonedaUpdateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Moneda
    form_class = MonedaForm
    template_name = 'nucleo/moneda_form.html'
    success_url = reverse_lazy('nucleo:moneda_list')

class MonedaDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Moneda
    template_name = 'nucleo/moneda_confirm_delete.html'
    success_url = reverse_lazy('nucleo:moneda_list')
