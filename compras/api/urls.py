from django.urls import path, include
from rest_framework.routers import DefaultRouter
from compras.api.devoluciones import DevolucionProveedorViewSet
from compras.api.necesidades import NecesidadCompraViewSet
from compras.api.views import (
    CalidadInspeccionViewSet,
    OrdenCompraViewSet,
    RecepcionViewSet,
    RecepcionRFIDEncuadreViewSet,
)

router = DefaultRouter()
router.register(r'ordenes', OrdenCompraViewSet, basename='ordenes-compra')
router.register(r'recepciones', RecepcionViewSet, basename='recepciones')
router.register(r'necesidades', NecesidadCompraViewSet, basename='necesidades-compra')
router.register(r'devoluciones-proveedor', DevolucionProveedorViewSet, basename='devoluciones-proveedor')
router.register(r'calidad-inspecciones', CalidadInspeccionViewSet, basename='calidad-inspecciones')
router.register(r'recepcion-rfid-encuadres', RecepcionRFIDEncuadreViewSet, basename='recepcion-rfid-encuadres')

urlpatterns = [
    path('', include(router.urls)),
]
