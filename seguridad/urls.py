from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import RolListView, RolCreateView, RolUpdateView, RolDeleteView, RolPermisosMatrixView, get_next_rol_id
from .api.api_views import RolViewSet

app_name = 'seguridad'

router = DefaultRouter()
router.register(r'roles', RolViewSet)

urlpatterns = [
    # API
    path('api/v1/seguridad/', include(router.urls)),

    # WEB CORE
    path('roles/', RolListView.as_view(), name='rol_list'),
    path('roles/crear/', RolCreateView.as_view(), name='rol_create'),
    path('roles/<int:pk>/editar/', RolUpdateView.as_view(), name='rol_update'),
    path('roles/<int:pk>/eliminar/', RolDeleteView.as_view(), name='rol_delete'),
    path('roles/permisos/', RolPermisosMatrixView.as_view(), name='rol_permisos_matrix'),
    path('ajax/roles/next-id/', get_next_rol_id, name='ajax_rol_next_id'),
]
