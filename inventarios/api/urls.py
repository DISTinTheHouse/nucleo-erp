from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import AlmacenViewSet, UbicacionViewSet

router = DefaultRouter()
router.register(r'almacenes', AlmacenViewSet, basename='almacen')
router.register(r'ubicaciones', UbicacionViewSet, basename='ubicacion')

urlpatterns = [
    path('', include(router.urls)),
]
