from django.shortcuts import render
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
    queryset = Empresa.objects.all()
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
    """
    queryset = Moneda.objects.all()
    serializer_class = MonedaSerializer
    lookup_field = 'codigo_iso'
    permission_classes = [IsSuperUserOrReadOnly]

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
