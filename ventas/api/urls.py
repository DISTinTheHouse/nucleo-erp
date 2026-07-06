from django.urls import path, include
from rest_framework import routers
from ventas.api.views import (
    CotizacionViewSet, 
    CotizacionDetalleViewSet, 
    PedidoViewSet, 
    PedidoDetalleViewSet, 
    PedidoDetalleTallaViewSet,
    MesaControlViewSet,
    ClienteViewSet
)

router = routers.DefaultRouter()
router.register(r'cotizaciones', CotizacionViewSet, basename='cotizaciones')
router.register(r'mesa-control', MesaControlViewSet, basename='mesa-control')
router.register(r'clientes', ClienteViewSet, basename='cliente')
router.register(r'cotizacion-detalle', CotizacionDetalleViewSet, basename='cotizaciondetalle')
router.register(r'pedidos', PedidoViewSet, basename='pedidos')
router.register(r'pedido-detalle', PedidoDetalleViewSet, basename='pedidodetalle')
router.register(r'pedido-detalle-talla', PedidoDetalleTallaViewSet, basename='pedidodetalletalla')

urlpatterns = [
    path('', include(router.urls)),
]
