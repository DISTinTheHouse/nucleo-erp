from django.urls import path, include
from rest_framework import routers
from produccion.api.views import ListaMaterialBomViewSet, OrdenProduccionViewSet, ConsumoProduccionViewSet, ProductoTerminadoEntradasViewSet

router = routers.DefaultRouter()
router.register(r'lista-material', ListaMaterialBomViewSet)
router.register(r'orden', OrdenProduccionViewSet)
router.register(r'consumo', ConsumoProduccionViewSet)
router.register(r'producto-terminado-entradas', ProductoTerminadoEntradasViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
