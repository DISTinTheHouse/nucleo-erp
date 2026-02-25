from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib import messages
from django.db.models import Q
from rest_framework import viewsets, permissions
from rest_framework.exceptions import PermissionDenied
from nucleo.mixins import AuditLogMixin
from seguridad.models import Permiso, UsuarioPermiso
from .models import Usuario
from .forms import UsuarioCreationForm, UsuarioChangeForm
from .serializers import UsuarioSerializer

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
class UsuarioViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar usuarios.
    """
    queryset = Usuario.objects.all()
    serializer_class = UsuarioSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        if hasattr(user, 'empresa') and user.empresa:
            return self.queryset.filter(empresa=user.empresa)
        return self.queryset.none()

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        
        # Superusuario: acceso total
        if request.user.is_superuser:
            return

        # Admin de Empresa: gestión limitada
        if getattr(request.user, 'is_admin_empresa', False):
            # Solo puede gestionar usuarios de su misma empresa
            if obj.empresa != request.user.empresa:
                raise PermissionDenied("No puedes gestionar usuarios de otra empresa.")
            
            # No puede editar/borrar superusuarios
            if obj.is_superuser:
                raise PermissionDenied("No puedes modificar a un superusuario.")
            
            return

        # Otros usuarios: solo lectura de su propio perfil o según permisos (aquí restringimos escritura)
        if request.method not in permissions.SAFE_METHODS:
             raise PermissionDenied("No tienes permisos para realizar esta acción.")

    def perform_create(self, serializer):
        user = self.request.user
        
        # Caso Superusuario
        if user.is_superuser:
            serializer.save()
            return

        # Caso Admin de Empresa
        if getattr(user, 'is_admin_empresa', False):
            # Validaciones de seguridad
            if serializer.validated_data.get('is_superuser', False):
                raise PermissionDenied("No puedes crear superusuarios.")
            
            if serializer.validated_data.get('is_admin_empresa', False):
                # Opcional: impedir crear otros admins o permitirlo con cuidado. 
                # Por seguridad default: bloqueado.
                raise PermissionDenied("No puedes crear otros administradores de empresa.")

            # Validar integridad de sucursal
            sucursal = serializer.validated_data.get('sucursal_default')
            if sucursal and sucursal.empresa != user.empresa:
                raise PermissionDenied("La sucursal seleccionada no pertenece a tu empresa.")

            # Forzar asignación a la empresa del admin
            serializer.save(empresa=user.empresa)
        else:
            raise PermissionDenied("No tienes permisos para crear usuarios.")

    def perform_update(self, serializer):
        user = self.request.user
        
        # Caso Superusuario
        if user.is_superuser:
            serializer.save()
            return

        # Caso Admin de Empresa
        if getattr(user, 'is_admin_empresa', False):
            # Validaciones de seguridad
            if serializer.validated_data.get('is_superuser', False):
                raise PermissionDenied("No puedes promover a superusuario.")
            
            # Impedir cambiar la empresa del usuario
            if 'empresa' in serializer.validated_data and serializer.validated_data['empresa'] != user.empresa:
                 raise PermissionDenied("No puedes mover usuarios a otra empresa.")

            # Validar integridad de sucursal si se está actualizando
            sucursal = serializer.validated_data.get('sucursal_default')
            if sucursal and sucursal.empresa != user.empresa:
                raise PermissionDenied("La sucursal seleccionada no pertenece a tu empresa.")

            serializer.save(empresa=user.empresa)
        else:
            raise PermissionDenied("No tienes permisos para editar usuarios.")

    def perform_destroy(self, instance):
        user = self.request.user
        if not user.is_superuser:
            if not getattr(user, 'is_admin_empresa', False):
                 raise PermissionDenied("No tienes permisos para eliminar usuarios.")
            if instance.is_superuser:
                 raise PermissionDenied("No puedes eliminar a un superusuario.")
            if instance.empresa != user.empresa:
                 raise PermissionDenied("No puedes eliminar usuarios de otra empresa.")
        
        super().perform_destroy(instance)

class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

class UsuarioListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Usuario
    template_name = 'usuarios/usuario_list.html'
    context_object_name = 'usuarios'

    def get_queryset(self):
        qs = (
            Usuario.objects.select_related("empresa")
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
