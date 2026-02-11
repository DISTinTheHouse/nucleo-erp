from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    UsuarioListView, UsuarioCreateView, UsuarioUpdateView, UsuarioDeleteView, UsuarioViewSet
)
from .api_views import LoginAPIView

app_name = 'usuarios'

router = DefaultRouter()
router.register(r'usuarios', UsuarioViewSet)

urlpatterns = [
    # Vistas Web (Core)
    path('core/usuarios/', UsuarioListView.as_view(), name='usuario_list'),
    path('core/usuarios/nuevo/', UsuarioCreateView.as_view(), name='usuario_create'),
    path('core/usuarios/<pk>/editar/', UsuarioUpdateView.as_view(), name='usuario_update'),
    path('core/usuarios/<pk>/eliminar/', UsuarioDeleteView.as_view(), name='usuario_delete'),
    
    # API Endpoints
    path('api/v1/login/', LoginAPIView.as_view(), name='api_login'),
    path('api/v1/', include(router.urls)),
]
