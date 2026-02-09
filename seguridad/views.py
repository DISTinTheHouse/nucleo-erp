from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from rest_framework import viewsets
from nucleo.mixins import AuditLogMixin
from .models import Rol
from .forms import RolForm
from .serializers import RolSerializer

# API
class RolViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar roles.
    """
    queryset = Rol.objects.all()
    serializer_class = RolSerializer

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

class RolListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Rol
    template_name = 'seguridad/rol_list.html'
    context_object_name = 'roles'
    paginate_by = 10

    def get_queryset(self):
        return Rol.objects.all().select_related('empresa').order_by('empresa', 'nombre')

class RolCreateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Rol
    form_class = RolForm
    template_name = 'seguridad/rol_form.html'
    success_url = reverse_lazy('seguridad:rol_list')

class RolUpdateView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Rol
    form_class = RolForm
    template_name = 'seguridad/rol_form.html'
    success_url = reverse_lazy('seguridad:rol_list')

class RolDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Rol
    template_name = 'seguridad/rol_confirm_delete.html'
    success_url = reverse_lazy('seguridad:rol_list')
