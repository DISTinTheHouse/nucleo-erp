from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AlmacenViewSet, UbicacionViewSet, ExistenciaViewSet, MovimientoInventarioViewSet, MovimientoInventarioDetalleViewSet, AjusteInventarioViewSet

router = DefaultRouter()
router.register(r'almacenes', AlmacenViewSet, basename='almacen')
router.register(r'ubicaciones', UbicacionViewSet, basename='ubicacion')
router.register(r'existencias', ExistenciaViewSet, basename='existencia')
router.register(r'movimientos', MovimientoInventarioViewSet, basename='movimiento')
router.register(r'movimientos-detalle', MovimientoInventarioDetalleViewSet, basename='movimiento-detalle')
router.register(r'ajustes-inventario', AjusteInventarioViewSet, basename='ajuste-inventario')

urlpatterns = [
    path('', include(router.urls)),
]
