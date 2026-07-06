from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import FacturaViewSet, ClienteViewSetContabilidad

router = DefaultRouter()
router.register(r'facturas', FacturaViewSet, basename='factura')
router.register(r'clientes-contabilidad', ClienteViewSetContabilidad, basename='cliente-contabilidad')

urlpatterns = [
    path('', include(router.urls)),
]