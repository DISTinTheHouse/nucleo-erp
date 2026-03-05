from django.urls import path, include
from rest_framework.routers import DefaultRouter
from terceros.api.views import ProveedorViewSet, ClienteViewSet

router = DefaultRouter()
router.register(r'proveedores', ProveedorViewSet, basename='proveedor')
router.register(r'clientes', ClienteViewSet, basename='cliente')

urlpatterns = [
    path('', include(router.urls)),
]
