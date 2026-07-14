from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CuentaPorCobrarViewSet, FacturaViewSet, ClienteViewSetContabilidad

router = DefaultRouter()
router.register(r'facturas', FacturaViewSet, basename='factura')
router.register(r'cuentas-por-cobrar', CuentaPorCobrarViewSet, basename='cuenta-por-cobrar')
router.register(r'clientes-contabilidad', ClienteViewSetContabilidad, basename='cliente-contabilidad')

urlpatterns = [
    path('', include(router.urls)),
]
