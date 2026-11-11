from produccion.api.salidas_maquila import SalidaMaquilaViewSet
from django.urls import path, include
from rest_framework import routers
from produccion.api.views import (
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
    CorteMangaAvancesViewSet,
    OrdenesCorteMangaViewSet,
    PedidoEspecialViewSet,
    VarianteProduccionViewSet,
    PreOrdenProduccionViewSet,
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
router.register(r'orden-corte-manga', OrdenesCorteMangaViewSet)
router.register(r'corte-manga-avances', CorteMangaAvancesViewSet)
router.register(r'pedidos-especiales', PedidoEspecialViewSet, basename='pedido-especial')
router.register(r'variantes-produccion', VarianteProduccionViewSet, basename='variante-produccion')
router.register(r'pre-ordenes', PreOrdenProduccionViewSet, basename='pre-orden-produccion')
router.register(r'salidas-maquila', SalidaMaquilaViewSet, basename='salida-maquila')

urlpatterns = [
    path('', include(router.urls)),
]
