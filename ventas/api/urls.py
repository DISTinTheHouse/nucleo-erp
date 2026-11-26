from django.urls import path, include
from rest_framework import routers
from ventas.api.oportunidades import OportunidadViewSet, SeguimientoViewSet
from ventas.api.stock_digital import StockDigitalViewSet
from ventas.api.views import (
    CotizacionViewSet, 
    CotizacionDetalleViewSet, 
    PedidoViewSet, 
    PedidoDetalleViewSet, 
    PedidoDetalleTallaViewSet,
    MesaControlViewSet
)

router = routers.DefaultRouter()
router.register(r'oportunidades', OportunidadViewSet, basename='oportunidades')
router.register(r'oportunidad-seguimientos', SeguimientoViewSet, basename='oportunidad-seguimientos')
router.register(r'cotizaciones', CotizacionViewSet, basename='cotizaciones')
router.register(r'mesa-control', MesaControlViewSet, basename='mesa-control')
router.register(r'cotizacion-detalle', CotizacionDetalleViewSet, basename='cotizaciondetalle')
router.register(r'pedidos', PedidoViewSet, basename='pedidos')
router.register(r'pedido-detalle', PedidoDetalleViewSet, basename='pedidodetalle')
router.register(r'pedido-detalle-talla', PedidoDetalleTallaViewSet, basename='pedidodetalletalla')
router.register(r'stock-digital', StockDigitalViewSet, basename='stock-digital')

urlpatterns = [
    path('', include(router.urls)),
]
