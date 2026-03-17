from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.http import JsonResponse
from django.views.generic import TemplateView, ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db.models import Max, Q

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from .models import (
    Empresa, Sucursal, Departamento, Moneda, Impuesto, UnidadMedida,
    SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago, SatClaveProdServ, SatClaveUnidad
)
from usuarios.models import Usuario
from .forms import EmpresaForm, SucursalForm, DepartamentoForm, MonedaForm, ImpuestoForm, UnidadMedidaForm
from .mixins import AuditLogMixin

@login_required
def get_sucursales_por_empresa(request, empresa_id):
    """
    Retorna JSON con las sucursales activas de una empresa específica,
    incluyendo sus departamentos activos y roles disponibles.
    """
    # 1. Sucursales y Departamentos
    # Seguridad adicional: verificar que el usuario tenga acceso a esta empresa
    user = request.user
    if not user.is_superuser:
        # Si no es admin de empresa ni tiene acceso explícito, denegar
        # (Lógica simplificada, idealmente checaríamos permisos detallados)
        if hasattr(user, 'empresa') and user.empresa and user.empresa.pk != empresa_id:
             return JsonResponse({'error': 'No autorizado'}, status=403)

    sucursales = Sucursal.objects.filter(empresa_id=empresa_id, activo=True).order_by('codigo')
    data_sucursales = []
    for s in sucursales:
        depts = s.departamentos.filter(activo=True).values('id_departamento', 'nombre', 'codigo')
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

@login_required
def get_next_empresa_id(request):
    if not request.user.is_superuser:
        return JsonResponse({'error': 'No autorizado'}, status=403)
    max_id = Empresa.objects.aggregate(m=Max('pk'))['m'] or 0
    next_id = max_id + 1
    return JsonResponse({'next_id': next_id, 'next_id_padded': f'{next_id:04d}'})

@login_required
def get_next_sucursal_id(request):
    if not request.user.is_superuser:
        return JsonResponse({'error': 'No autorizado'}, status=403)
    max_id = Sucursal.objects.aggregate(m=Max('pk'))['m'] or 0
    next_id = max_id + 1
    return JsonResponse({'next_id': next_id, 'next_id_padded': f'{next_id:04d}'})

@login_required
def get_next_departamento_id(request):
    if not request.user.is_superuser:
        return JsonResponse({'error': 'No autorizado'}, status=403)
    max_id = Departamento.objects.aggregate(m=Max('pk'))['m'] or 0
    next_id = max_id + 1
    return JsonResponse({'next_id': next_id, 'next_id_padded': f'{next_id:04d}'})
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
        try:
            from inventarios.models import Almacen, Ubicacion
            context['total_almacenes'] = Almacen.objects.count()
            context['total_ubicaciones'] = Ubicacion.objects.count()
        except Exception:
            context['total_almacenes'] = 0
            context['total_ubicaciones'] = 0
        return context

# --- EMPRESA ---
class EmpresaListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Empresa
    template_name = 'nucleo/empresa_list.html'
    context_object_name = 'empresas'
    ordering = ['-created_at']

    def get_queryset(self):
        # Garantizar que se retornen todas las empresas para el superusuario
        qs = Empresa.objects.all().order_by('-created_at')
        
        q = self.request.GET.get('q')
        if q:
            qs = qs.filter(
                Q(razon_social__icontains=q) | 
                Q(nombre_comercial__icontains=q) | 
                Q(rfc__icontains=q) |
                Q(codigo__icontains=q)
            )
        return qs

class EmpresaCreateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Empresa
    form_class = EmpresaForm
    template_name = 'nucleo/empresa_form.html'
    success_url = reverse_lazy('nucleo:empresa_list')
    success_message = "La empresa %(razon_social)s fue creada correctamente."
    
    def form_valid(self, form):
        from django.utils.text import slugify
        # Guardar provisionalmente con prefix-0000 y luego actualizar a prefix-<id:04d>
        self.object = form.save(commit=False)
        base_text = form.cleaned_data.get('nombre_comercial') or form.cleaned_data.get('razon_social') or ''
        base_slug = slugify(base_text)
        prefix = base_slug[:10].strip('-')
        if not prefix:
            form.add_error('nombre_comercial', 'Proporciona un Nombre Comercial para generar el código.')
            return self.form_invalid(form)
        provisional = f"{prefix}-0000"
        self.object.codigo = provisional
        self.object.save()
        final_codigo = f"{prefix}-{self.object.pk:04d}"
        if self.object.codigo != final_codigo:
            self.object.codigo = final_codigo
            self.object.save(update_fields=["codigo"])
        form.save_m2m()
        return super().form_valid(form)

class EmpresaUpdateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Empresa
    form_class = EmpresaForm
    template_name = 'nucleo/empresa_form.html'
    success_url = reverse_lazy('nucleo:empresa_list')
    success_message = "La empresa %(razon_social)s fue editada correctamente."

class EmpresaDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Empresa
    template_name = 'nucleo/empresa_confirm_delete.html'
    success_url = reverse_lazy('nucleo:empresa_list')

# --- SUCURSAL ---
class SucursalListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Sucursal
    template_name = 'nucleo/sucursal_list.html'
    context_object_name = 'sucursales'

    def get_queryset(self):
        qs = (
            Sucursal.objects.select_related("empresa")
            .all()
            .order_by("empresa__codigo", "codigo")
        )
        q = self.request.GET.get('q')
        if q:
            qs = qs.filter(
                Q(nombre__icontains=q) | 
                Q(codigo__icontains=q) |
                Q(empresa__razon_social__icontains=q)
            )
        return qs

class SucursalCreateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Sucursal
    form_class = SucursalForm
    template_name = 'nucleo/sucursal_form.html'
    success_url = reverse_lazy('nucleo:sucursal_list')
    success_message = "La sucursal %(nombre)s fue creada correctamente."
    
    def form_valid(self, form):
        from django.utils.text import slugify
        self.object = form.save(commit=False)
        base_text = form.cleaned_data.get('nombre') or ''
        base_slug = slugify(base_text)
        prefix = base_slug[:10].strip('-')
        if not prefix:
            form.add_error('nombre', 'Proporciona un Nombre para generar el código.')
            return self.form_invalid(form)
        provisional = f"{prefix}-0000"
        self.object.codigo = provisional
        self.object.save()
        final_codigo = f"{prefix}-{self.object.pk:04d}"
        if self.object.codigo != final_codigo:
            self.object.codigo = final_codigo
            self.object.save(update_fields=["codigo"])
        form.save_m2m()
        return super().form_valid(form)

class SucursalUpdateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Sucursal
    form_class = SucursalForm
    template_name = 'nucleo/sucursal_form.html'
    success_url = reverse_lazy('nucleo:sucursal_list')
    success_message = "La sucursal %(nombre)s fue editada correctamente."

class SucursalDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Sucursal
    template_name = 'nucleo/sucursal_confirm_delete.html'
    success_url = reverse_lazy('nucleo:sucursal_list')

# --- DEPARTAMENTO ---
class DepartamentoListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Departamento
    template_name = 'nucleo/departamento_list.html'
    context_object_name = 'departamentos'

    def get_queryset(self):
        qs = (
            Departamento.objects.select_related("empresa", "sucursal")
            .all()
            .order_by("empresa__codigo", "sucursal__codigo", "codigo")
        )
        q = self.request.GET.get('q')
        if q:
            qs = qs.filter(
                Q(nombre__icontains=q) | 
                Q(codigo__icontains=q) |
                Q(empresa__razon_social__icontains=q) |
                Q(sucursal__nombre__icontains=q)
            )
        return qs

class DepartamentoCreateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Departamento
    form_class = DepartamentoForm
    template_name = 'nucleo/departamento_form.html'
    success_url = reverse_lazy('nucleo:departamento_list')
    success_message = "El departamento %(nombre)s fue creado correctamente."
    
    def form_valid(self, form):
        from django.utils.text import slugify
        self.object = form.save(commit=False)
        base_text = form.cleaned_data.get('nombre') or ''
        base_slug = slugify(base_text)
        prefix = base_slug[:10].strip('-')
        if not prefix:
            form.add_error('nombre', 'Proporciona un Nombre para generar el código.')
            return self.form_invalid(form)
        provisional = f"{prefix}-0000"
        self.object.codigo = provisional
        self.object.save()
        final_codigo = f"{prefix}-{self.object.pk:04d}"
        if self.object.codigo != final_codigo:
            self.object.codigo = final_codigo
            self.object.save(update_fields=["codigo"])
        form.save_m2m()
        return super().form_valid(form)

class DepartamentoUpdateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Departamento
    form_class = DepartamentoForm
    template_name = 'nucleo/departamento_form.html'
    success_url = reverse_lazy('nucleo:departamento_list')
    success_message = "El departamento %(nombre)s fue editado correctamente."

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
