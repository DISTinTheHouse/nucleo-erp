from django.urls import path, include
from rest_framework.routers import DefaultRouter
from wms.api.views import TransferenciaViewSet

router = DefaultRouter()
router.register(r'transferencias', TransferenciaViewSet, basename='transferencias')

urlpatterns = [
    path('', include(router.urls)),
]
