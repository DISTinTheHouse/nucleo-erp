from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from rest_framework import viewsets, permissions
from nucleo.mixins import AuditLogMixin
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
    permission_classes = [IsSuperUserOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        if hasattr(user, 'empresa') and user.empresa:
            return self.queryset.filter(empresa=user.empresa)
        return self.queryset.none()

class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

class UsuarioListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Usuario
    template_name = 'usuarios/usuario_list.html'
    context_object_name = 'usuarios'

class UsuarioCreateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Usuario
    form_class = UsuarioCreationForm
    template_name = 'usuarios/usuario_form.html'
    success_url = reverse_lazy('usuarios:usuario_list')

class UsuarioUpdateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Usuario
    form_class = UsuarioChangeForm
    template_name = 'usuarios/usuario_form.html'
    success_url = reverse_lazy('usuarios:usuario_list')

class UsuarioDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Usuario
    template_name = 'usuarios/usuario_confirm_delete.html'
    success_url = reverse_lazy('usuarios:usuario_list')
