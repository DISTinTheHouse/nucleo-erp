from django.urls import path
from .views import (
    AlmacenListView,
    UbicacionListView,
)

app_name = 'inventarios'

urlpatterns = [
    path('core/almacenes/', AlmacenListView.as_view(), name='almacen_list'),
    path('core/ubicaciones/', UbicacionListView.as_view(), name='ubicacion_list'),
]
