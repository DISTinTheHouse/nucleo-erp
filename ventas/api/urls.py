from django.urls import path, include
from rest_framework import routers
from ventas.api.views import CotizacionViewSet, CotizacionDetalleViewSet, PedidoViewSet, PedidoDetalleViewSet

router = routers.DefaultRouter()
router.register(r'cotizaciones', CotizacionViewSet, basename='cotizaciones')
router.register(r'cotizacion-detalle', CotizacionDetalleViewSet, basename='cotizaciondetalle')
router.register(r'pedidos', PedidoViewSet, basename='pedidos')
router.register(r'pedido-detalle', PedidoDetalleViewSet, basename='pedidodetalle')

urlpatterns = [
    path('', include(router.urls)),
]
