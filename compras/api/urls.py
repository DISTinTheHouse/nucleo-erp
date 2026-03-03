from django.urls import path, include
from rest_framework.routers import DefaultRouter
from compras.api.views import OrdenCompraViewSet, RecepcionViewSet

router = DefaultRouter()
router.register(r'ordenes', OrdenCompraViewSet, basename='ordenes-compra')
router.register(r'recepciones', RecepcionViewSet, basename='recepciones')

urlpatterns = [
    path('', include(router.urls)),
]
