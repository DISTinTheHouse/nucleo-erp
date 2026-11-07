from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .costos import CostoInventarioViewSet
from .views import AlmacenViewSet, UbicacionViewSet, ExistenciaViewSet, OperacionInventarioViewSet, MovimientoOperacionViewSet, SkuMuestraViewSet

router = DefaultRouter()
router.trailing_slash = "/?"
router.register(r'almacenes', AlmacenViewSet, basename='almacen')
router.register(r'ubicaciones', UbicacionViewSet, basename='ubicacion')
router.register(r'existencias', ExistenciaViewSet, basename='existencia')
router.register(r'operaciones', OperacionInventarioViewSet, basename='operacion-inventario')
router.register(r'movimientos', MovimientoOperacionViewSet, basename='movimiento')
router.register(r'skus-muestra', SkuMuestraViewSet, basename='sku-muestra')
router.register(r'costos', CostoInventarioViewSet, basename='costo-inventario')

urlpatterns = [
    path('', include(router.urls)),
]
