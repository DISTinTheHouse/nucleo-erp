from django.urls import path, include
from rest_framework.routers import DefaultRouter
from catalogo.api.views import TipoProductoViewSet, CategoriaProductoViewSet, ColorViewSet, TallaViewSet, ProductoViewSet, ProductoVarianteViewSet, CategoriaProductoProcesoViewSet

tipo_producto_router = DefaultRouter()
tipo_producto_router.register(prefix='tipo-producto', viewset=TipoProductoViewSet, basename='tipo-producto')

categoria_producto_router = DefaultRouter()
categoria_producto_router.register(prefix='categoria-producto', viewset=CategoriaProductoViewSet, basename='categoria-producto')

categoria_proceso_router = DefaultRouter()
categoria_proceso_router.register(prefix='categoria-producto-proceso', viewset=CategoriaProductoProcesoViewSet, basename='categoria-producto-proceso')

color_router = DefaultRouter()
color_router.register(prefix='color', viewset=ColorViewSet, basename='color')

talla_router = DefaultRouter()
talla_router.register(prefix='talla', viewset=TallaViewSet, basename='talla')

producto_router = DefaultRouter()
producto_router.register(prefix='producto', viewset=ProductoViewSet, basename='producto')

producto_variante_router = DefaultRouter()
producto_variante_router.register(prefix='producto-variante', viewset=ProductoVarianteViewSet, basename='producto-variante')

urlpatterns = [
    path('', include(tipo_producto_router.urls)),
    path('', include(categoria_producto_router.urls)),
    path('', include(categoria_proceso_router.urls)),
    path('', include(color_router.urls)),
    path('', include(talla_router.urls)),
    path('', include(producto_router.urls)),
    path('', include(producto_variante_router.urls)),
]
