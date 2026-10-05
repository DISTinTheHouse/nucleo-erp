from django.urls import path, include
from rest_framework.routers import DefaultRouter
from compras.api.views import (
    CalidadInspeccionViewSet,
    ComprasDashboardView,
    OrdenCompraViewSet,
    RecepcionViewSet,
    RecepcionRFIDEncuadreViewSet,
)

router = DefaultRouter()
router.register(r'ordenes', OrdenCompraViewSet, basename='ordenes-compra')
router.register(r'recepciones', RecepcionViewSet, basename='recepciones')
router.register(r'calidad-inspecciones', CalidadInspeccionViewSet, basename='calidad-inspecciones')
router.register(r'recepcion-rfid-encuadres', RecepcionRFIDEncuadreViewSet, basename='recepcion-rfid-encuadres')

urlpatterns = [
    path('dashboard/', ComprasDashboardView.as_view(), name='compras-dashboard'),
    path('', include(router.urls)),
]
