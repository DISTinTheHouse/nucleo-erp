from django.urls import path
from .views import (
    AlmacenListView, AlmacenCreateView, AlmacenUpdateView, AlmacenDeleteView,
    UbicacionListView, UbicacionCreateView, UbicacionUpdateView, UbicacionDeleteView
)

app_name = 'inventarios'

urlpatterns = [
    path('core/almacenes/', AlmacenListView.as_view(), name='almacen_list'),
    path('core/almacenes/nuevo/', AlmacenCreateView.as_view(), name='almacen_create'),
    path('core/almacenes/<pk>/editar/', AlmacenUpdateView.as_view(), name='almacen_update'),
    path('core/almacenes/<pk>/eliminar/', AlmacenDeleteView.as_view(), name='almacen_delete'),

    path('core/ubicaciones/', UbicacionListView.as_view(), name='ubicacion_list'),
    path('core/ubicaciones/nuevo/', UbicacionCreateView.as_view(), name='ubicacion_create'),
    path('core/ubicaciones/<pk>/editar/', UbicacionUpdateView.as_view(), name='ubicacion_update'),
    path('core/ubicaciones/<pk>/eliminar/', UbicacionDeleteView.as_view(), name='ubicacion_delete'),
]

