from django.urls import path, include
from rest_framework.routers import DefaultRouter
from terceros.api.views import ProveedorViewSet, ClienteViewSet, ClienteViewSetMesaControl, DireccionClienteViewSet

router = DefaultRouter()
router.register(r'proveedores', ProveedorViewSet, basename='proveedor')
router.register(r'clientes', ClienteViewSet, basename='cliente')
router.register(r'clientes-mesa-control', ClienteViewSetMesaControl, basename='cliente-mesa-control')
router.register(r'direcciones-clientes', DireccionClienteViewSet, basename='direccion-cliente')

urlpatterns = [
    path('', include(router.urls)),
]
