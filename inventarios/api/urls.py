from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AlmacenViewSet, UbicacionViewSet, ExistenciaViewSet, MovimientoInventarioViewSet

router = DefaultRouter()
router.register(r'almacenes', AlmacenViewSet, basename='almacen')
router.register(r'ubicaciones', UbicacionViewSet, basename='ubicacion')
router.register(r'existencias', ExistenciaViewSet, basename='existencia')
router.register(r'movimientos', MovimientoInventarioViewSet, basename='movimiento')

urlpatterns = [
    path('', include(router.urls)),
]
