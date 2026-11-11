from rest_framework.routers import DefaultRouter

from .views import BitacoraDocumentoViewSet

router = DefaultRouter()
router.register("documentos", BitacoraDocumentoViewSet, basename="bitacora-documentos")
urlpatterns = router.urls
