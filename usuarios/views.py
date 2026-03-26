from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib import messages
from django.db.models import Q
from nucleo.mixins import AuditLogMixin
from seguridad.models import Permiso, UsuarioPermiso
from .models import Usuario
from .forms import UsuarioCreationForm, UsuarioChangeForm

class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

class UsuarioListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Usuario
    template_name = 'usuarios/usuario_list.html'
    context_object_name = 'usuarios'

    def get_queryset(self):
        qs = (
            Usuario.objects.select_related("empresa").prefetch_related("asignaciones_roles__rol")
            .all()
            .order_by("empresa__codigo", "username")
        )
        q = self.request.GET.get('q')
        if q:
            qs = qs.filter(
                Q(username__icontains=q) | 
                Q(first_name__icontains=q) | 
                Q(last_name__icontains=q) | 
                Q(email__icontains=q) |
                Q(empresa__razon_social__icontains=q)
            )
        return qs

class UsuarioCreateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Usuario
    form_class = UsuarioCreationForm
    template_name = 'usuarios/usuario_form.html'
    success_url = reverse_lazy('usuarios:usuario_list')
    success_message = "El usuario %(username)s fue creado correctamente."

class UsuarioUpdateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Usuario
    form_class = UsuarioChangeForm
    template_name = 'usuarios/usuario_form.html'
    success_url = reverse_lazy('usuarios:usuario_list')
    success_message = "El usuario %(username)s fue editado correctamente."

class UsuarioDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Usuario
    template_name = 'usuarios/usuario_confirm_delete.html'
    success_url = reverse_lazy('usuarios:usuario_list')
    success_message = "El usuario %(username)s fue eliminado correctamente."

class UsuarioPermisosView(LoginRequiredMixin, SuperuserRequiredMixin, View):
    template_name = 'usuarios/usuario_permisos.html'

    def get(self, request, pk):
        usuario = get_object_or_404(Usuario, pk=pk)
        
        permisos = Permiso.objects.all().order_by('modulo', 'clave')
        
        permisos_by_modulo = {}
        for p in permisos:
            mod = p.modulo if p.modulo else 'General'
            if mod not in permisos_by_modulo:
                permisos_by_modulo[mod] = []
            permisos_by_modulo[mod].append(p)
            
        overrides = {
            up.permiso_id: up.tipo 
            for up in usuario.overrides_permisos.all()
        }
        
        context = {
            'usuario': usuario,
            'permisos_by_modulo': permisos_by_modulo,
            'overrides': overrides,
            'GRANT': UsuarioPermiso.TIPO_GRANT,
            'DENY': UsuarioPermiso.TIPO_DENY,
        }
        return render(request, self.template_name, context)

    def post(self, request, pk):
        usuario = get_object_or_404(Usuario, pk=pk)
        
        # Contexto de empresa
        empresa_ctx = usuario.empresa
        if not empresa_ctx and hasattr(request.user, 'empresa'):
            empresa_ctx = request.user.empresa

        # Procesar formulario
        permisos = Permiso.objects.all()
        
        for permiso in permisos:
            field_name = f'permiso_{permiso.id}'
            valor = request.POST.get(field_name)
            
            if not valor:
                continue
                
            override = UsuarioPermiso.objects.filter(usuario=usuario, permiso=permiso).first()
            
            if valor == 'default':
                if override:
                    override.delete()
            elif valor in [UsuarioPermiso.TIPO_GRANT, UsuarioPermiso.TIPO_DENY]:
                if override:
                    if override.tipo != valor:
                        override.tipo = valor
                        override.save()
                else:
                    UsuarioPermiso.objects.create(
                        usuario=usuario,
                        permiso=permiso,
                        tipo=valor,
                        empresa=empresa_ctx
                    )
        
        messages.success(request, f"Permisos actualizados para {usuario.username}")
        return redirect('usuarios:usuario_permisos', pk=pk)
    success_url = reverse_lazy('usuarios:usuario_list')
