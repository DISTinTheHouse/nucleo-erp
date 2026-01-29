from django.urls import path
from .views import (
    UsuarioListView, UsuarioCreateView, UsuarioUpdateView, UsuarioDeleteView
)

app_name = 'usuarios'

urlpatterns = [
    path('core/usuarios/', UsuarioListView.as_view(), name='usuario_list'),
    path('core/usuarios/nuevo/', UsuarioCreateView.as_view(), name='usuario_create'),
    path('core/usuarios/<pk>/editar/', UsuarioUpdateView.as_view(), name='usuario_update'),
    path('core/usuarios/<pk>/eliminar/', UsuarioDeleteView.as_view(), name='usuario_delete'),
]
