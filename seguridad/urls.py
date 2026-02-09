from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import RolListView, RolCreateView, RolUpdateView, RolDeleteView, RolViewSet

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
]
