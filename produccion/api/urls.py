from django.urls import path, include
from rest_framework import routers
from produccion.api.views import (
    OrdenBordadoViewSet,
    ListaMaterialBomViewSet,
    BomDetalleViewSet, 
    ConsumoProduccionViewSet, 
    OrdenProduccionViewSet, 
    ProductoTerminadoEntradasViewSet,
    OrdenBordadoViewSet,
    BordadoAvancesViewSet,
    BordadoIncidenciasViewSet,
    OrdenReflejanteViewSet,
    ReflejanteAvancesViewSet,
    ReflejanteIncidenciasViewSet,
)

router = routers.DefaultRouter()
router.register(r'lista-material', ListaMaterialBomViewSet, basename='lista-material-bom')
router.register(r'bom-detalle', BomDetalleViewSet, basename='bom-detalle')
router.register(r'orden-produccion', OrdenProduccionViewSet)
router.register(r'consumo', ConsumoProduccionViewSet)
router.register(r'producto-terminado-entradas', ProductoTerminadoEntradasViewSet)
router.register(r'orden-bordado', OrdenBordadoViewSet)
router.register(r'bordado-avances', BordadoAvancesViewSet)
router.register(r'bordado-incidencias', BordadoIncidenciasViewSet)
router.register(r'orden-reflejante', OrdenReflejanteViewSet)
router.register(r'reflejante-avances', ReflejanteAvancesViewSet)
router.register(r'reflejante-incidencias', ReflejanteIncidenciasViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
