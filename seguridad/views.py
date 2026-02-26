from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView, TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib import messages
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from nucleo.mixins import AuditLogMixin
from .models import Rol, Permiso
from .forms import RolForm
from .serializers import RolSerializer, RolPermisosSerializer

# Custom Permission
class IsSuperUserOrReadOnly(permissions.BasePermission):
    """
    Permite acceso total a superusuarios.
    Lectura permitida a usuarios autenticados (sujeta a filtros de queryset).
    Escritura prohibida para no superusuarios.
    """
    def has_permission(self, request, view):
        # Permitir métodos seguros a autenticados
        if request.method in permissions.SAFE_METHODS:
            return request.user and request.user.is_authenticated
            
        # Para acciones personalizadas de permisos (POST/PUT), permitir si es superusuario o admin de empresa
        if view.action == 'permisos':
            return request.user and request.user.is_authenticated and (request.user.is_superuser or getattr(request.user, 'is_admin_empresa', False))
            
        return request.user and request.user.is_superuser

# API
class RolViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar roles.
    """
    queryset = Rol.objects.all()
    serializer_class = RolSerializer
    permission_classes = [IsSuperUserOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return self.queryset
        if hasattr(user, 'empresa') and user.empresa:
            return self.queryset.filter(empresa=user.empresa)
        return self.queryset.none()

    @action(detail=True, methods=['get', 'put'], serializer_class=RolPermisosSerializer)
    def permisos(self, request, pk=None):
        """
        Endpoint para gestionar los permisos de un rol específico.
        GET: Retorna los IDs de permisos asignados.
        PUT: Actualiza la lista de permisos asignados (recibe lista de IDs).
        """
        rol = self.get_object()
        
        if request.method == 'GET':
            # Retornar lista de IDs de permisos asignados
            permisos_ids = rol.permisos.values_list('id', flat=True)
            return Response({'permisos': list(permisos_ids)})
            
        elif request.method == 'PUT':
            serializer = self.get_serializer(rol, data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response({'status': 'Permisos actualizados correctamente', 'permisos': request.data.get('permisos', [])})
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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

class RolCreateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Rol
    form_class = RolForm
    template_name = 'seguridad/rol_form.html'
    success_url = reverse_lazy('seguridad:rol_list')
    success_message = "El rol %(nombre)s fue creado correctamente."
    
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

class RolUpdateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Rol
    form_class = RolForm
    template_name = 'seguridad/rol_form.html'
    success_url = reverse_lazy('seguridad:rol_list')
    success_message = "El rol %(nombre)s fue editado correctamente."

from django.http import JsonResponse
from django.db.models import Max

from django.contrib.auth.decorators import login_required

@login_required
def get_next_rol_id(request):
    if not request.user.is_superuser:
        return JsonResponse({'error': 'No autorizado'}, status=403)
    max_id = Rol.objects.aggregate(m=Max('pk'))['m'] or 0
    next_id = max_id + 1
    return JsonResponse({'next_id': next_id, 'next_id_padded': f'{next_id:04d}'})

class RolDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Rol
    template_name = 'seguridad/rol_confirm_delete.html'
    success_url = reverse_lazy('seguridad:rol_list')


class RolPermisosMatrixView(LoginRequiredMixin, SuperuserRequiredMixin, TemplateView):
    template_name = 'seguridad/rol_permisos_matrix.html'

    def get_empresa(self):
        return getattr(self.request.user, "empresa", None)

    def get_rol_queryset(self):
        qs = Rol.objects.all().select_related("empresa")
        # Filtro por empresa explícita (solo para superusuarios)
        empresa_id = self.request.GET.get("empresa_id") or self.request.POST.get("empresa_id")
        if self.request.user.is_superuser and empresa_id:
            try:
                qs = qs.filter(empresa_id=int(empresa_id))
            except ValueError:
                pass
        else:
            # Para usuarios no superusuarios (por si cambiamos permisos), filtrar por su empresa asignada
            empresa = self.get_empresa()
            if empresa and not self.request.user.is_superuser:
                qs = qs.filter(empresa=empresa)
        return qs.filter(estatus=Rol.Estatus.ACTIVO).order_by("empresa__codigo", "nombre")

    def get_rol(self):
        qs = self.get_rol_queryset()
        if not qs.exists():
            return None
        rol_id = self.request.GET.get("rol_id") or self.request.POST.get("rol_id")
        if rol_id:
            obj = qs.filter(pk=rol_id).first()
            if obj:
                return obj
        return qs.first()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        empresa = self.get_empresa()
        roles_qs = self.get_rol_queryset()
        rol = self.get_rol()
        permisos = Permiso.objects.all().order_by("modulo", "clave")
        asignados = set()
        if rol:
            asignados = set(rol.permisos.values_list("id", flat=True))
        # Opciones de empresas para superusuario (derivadas de roles activos)
        empresas_opts = []
        if self.request.user.is_superuser:
            empresas_raw = roles_qs.values("empresa_id", "empresa__codigo").distinct().order_by("empresa__codigo")
            empresas_opts = [{"id": e["empresa_id"], "codigo": e["empresa__codigo"]} for e in empresas_raw]
        empresa_seleccionada_id = self.request.GET.get("empresa_id") or self.request.POST.get("empresa_id") or ""
        context.update(
            empresa=empresa,
            roles=roles_qs,
            rol_seleccionado=rol,
            permisos=permisos,
            permisos_asignados=asignados,
            empresas=empresas_opts,
            empresa_seleccionada_id=empresa_seleccionada_id,
        )
        return context

    def post(self, request, *args, **kwargs):
        rol = self.get_rol()
        if not rol:
            return self.get(request, *args, **kwargs)
        permisos_ids = request.POST.getlist("permiso_ids")
        permisos_qs = Permiso.objects.filter(id__in=permisos_ids)
        rol.permisos.set(permisos_qs)
        messages.success(request, f"Permisos actualizados correctamente para el rol {rol.nombre}.")
        return self.get(request, *args, **kwargs)
