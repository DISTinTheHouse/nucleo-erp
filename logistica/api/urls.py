from django.urls import path, include
from rest_framework.routers import DefaultRouter
from logistica.api.views import EnvioViewSet

router = DefaultRouter()
router.register(r'envios', EnvioViewSet, basename='envios')

urlpatterns = [
    path('', include(router.urls)),
]
