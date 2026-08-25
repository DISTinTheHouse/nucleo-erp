from django.urls import path, include
from rest_framework import routers
from ventas.api.views import (
    CotizacionViewSet, 
    CotizacionDetalleViewSet, 
    PedidoViewSet, 
    PedidoDetalleViewSet, 
    PedidoDetalleTallaViewSet,
    MesaControlViewSet,
    ProspectoViewSet,
    OportunidadViewSet,
    EntregaViewSet,
    DevolucionViewSet,
    BackorderViewSet,
    ActividadCrmViewSet
)

router = routers.DefaultRouter()
router.register(r'cotizaciones', CotizacionViewSet, basename='cotizaciones')
router.register(r'mesa-control', MesaControlViewSet, basename='mesa-control')
router.register(r'cotizacion-detalle', CotizacionDetalleViewSet, basename='cotizaciondetalle')
router.register(r'pedidos', PedidoViewSet, basename='pedidos')
router.register(r'pedido-detalle', PedidoDetalleViewSet, basename='pedidodetalle')
router.register(r'pedido-detalle-talla', PedidoDetalleTallaViewSet, basename='pedidodetalletalla')
router.register(r'prospectos', ProspectoViewSet, basename='prospectos')
router.register(r'oportunidades', OportunidadViewSet, basename='oportunidades')
router.register(r'entregas', EntregaViewSet, basename='entregas')
router.register(r'devoluciones', DevolucionViewSet, basename='devoluciones')
router.register(r'backorders', BackorderViewSet, basename='backorders')
router.register(r'actividades-crm', ActividadCrmViewSet, basename='actividades-crm')

urlpatterns = [
    path('', include(router.urls)),
]
