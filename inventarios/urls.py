from django.urls import path
from .views import (
    AlmacenListView,
    UbicacionListView,
    wms_demo,
)

app_name = 'inventarios'

urlpatterns = [
    path('core/almacenes/', AlmacenListView.as_view(), name='almacen_list'),
    path('core/ubicaciones/', UbicacionListView.as_view(), name='ubicacion_list'),
    path('core/wms-demo/', wms_demo, name='wms_demo'),
]
