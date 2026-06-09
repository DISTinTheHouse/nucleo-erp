from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AlmacenViewSet, UbicacionViewSet, ExistenciaViewSet, OperacionInventarioViewSet

router = DefaultRouter()
router.register(r'almacenes', AlmacenViewSet, basename='almacen')
router.register(r'ubicaciones', UbicacionViewSet, basename='ubicacion')
router.register(r'existencias', ExistenciaViewSet, basename='existencia')
router.register(r'operaciones', OperacionInventarioViewSet, basename='operacion-inventario')

urlpatterns = [
    path('', include(router.urls)),
]
