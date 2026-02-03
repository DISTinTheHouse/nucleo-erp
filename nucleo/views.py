from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import TemplateView, ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from rest_framework import viewsets

from .models import Empresa, Sucursal, Departamento, Moneda, Impuesto, UnidadMedida
from .serializers import EmpresaSerializer
from .forms import EmpresaForm, SucursalForm, DepartamentoForm, MonedaForm, ImpuestoForm, UnidadMedidaForm

# API
class EmpresaViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar empresas.
    """
    queryset = Empresa.objects.all()
    serializer_class = EmpresaSerializer
    lookup_field = 'codigo'

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
        context['total_usuarios'] = 0 # Placeholder, fetched in template or separate context
        return context

# --- EMPRESA ---
class EmpresaListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Empresa
    template_name = 'nucleo/empresa_list.html'
    context_object_name = 'empresas'

class EmpresaCreateView(LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Empresa
    form_class = EmpresaForm
    template_name = 'nucleo/empresa_form.html'
    success_url = reverse_lazy('nucleo:empresa_list')

class EmpresaUpdateView(LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Empresa
    form_class = EmpresaForm
    template_name = 'nucleo/empresa_form.html'
    success_url = reverse_lazy('nucleo:empresa_list')

class EmpresaDeleteView(LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Empresa
    template_name = 'nucleo/empresa_confirm_delete.html'
    success_url = reverse_lazy('nucleo:empresa_list')

# --- SUCURSAL ---
class SucursalListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Sucursal
    template_name = 'nucleo/sucursal_list.html'
    context_object_name = 'sucursales'

class SucursalCreateView(LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Sucursal
    form_class = SucursalForm
    template_name = 'nucleo/sucursal_form.html'
    success_url = reverse_lazy('nucleo:sucursal_list')

class SucursalUpdateView(LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Sucursal
    form_class = SucursalForm
    template_name = 'nucleo/sucursal_form.html'
    success_url = reverse_lazy('nucleo:sucursal_list')

class SucursalDeleteView(LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Sucursal
    template_name = 'nucleo/sucursal_confirm_delete.html'
    success_url = reverse_lazy('nucleo:sucursal_list')

# --- DEPARTAMENTO ---
class DepartamentoListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Departamento
    template_name = 'nucleo/departamento_list.html'
    context_object_name = 'departamentos'

class DepartamentoCreateView(LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Departamento
    form_class = DepartamentoForm
    template_name = 'nucleo/departamento_form.html'
    success_url = reverse_lazy('nucleo:departamento_list')

class DepartamentoUpdateView(LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Departamento
    form_class = DepartamentoForm
    template_name = 'nucleo/departamento_form.html'
    success_url = reverse_lazy('nucleo:departamento_list')

class DepartamentoDeleteView(LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Departamento
    template_name = 'nucleo/departamento_confirm_delete.html'
    success_url = reverse_lazy('nucleo:departamento_list')

# --- MONEDA ---
class MonedaListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Moneda
    template_name = 'nucleo/moneda_list.html'
    context_object_name = 'monedas'

class MonedaCreateView(LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Moneda
    form_class = MonedaForm
    template_name = 'nucleo/moneda_form.html'
    success_url = reverse_lazy('nucleo:moneda_list')

class MonedaUpdateView(LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Moneda
    form_class = MonedaForm
    template_name = 'nucleo/moneda_form.html'
    success_url = reverse_lazy('nucleo:moneda_list')

class MonedaDeleteView(LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Moneda
    template_name = 'nucleo/moneda_confirm_delete.html'
    success_url = reverse_lazy('nucleo:moneda_list')
