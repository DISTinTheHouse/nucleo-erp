from django.urls import path, include
from rest_framework.routers import DefaultRouter
from wms.api.views import DespachoViewSet, TransferenciaViewSet, PickingViewSet, PackingViewSet, EtiquetaRFIDViewSet

router = DefaultRouter()
router.register(r'transferencias', TransferenciaViewSet, basename='transferencias')
router.register(r'pickings', PickingViewSet, basename='pickings')
router.register(r'packings', PackingViewSet, basename='packings')
router.register(r'despachos', DespachoViewSet, basename='despachos')
router.register(r'etiquetas-rfid', EtiquetaRFIDViewSet, basename='etiquetas-rfid')

urlpatterns = [
    path('', include(router.urls)),
]
