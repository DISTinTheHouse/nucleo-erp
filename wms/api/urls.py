from django.urls import path, include
from rest_framework.routers import DefaultRouter
from wms.api.views import TransferenciaViewSet, PickingViewSet

router = DefaultRouter()
router.register(r'transferencias', TransferenciaViewSet, basename='transferencias')
router.register(r'pickings', PickingViewSet, basename='pickings')

urlpatterns = [
    path('', include(router.urls)),
]
