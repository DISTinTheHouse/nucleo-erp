from rest_framework import viewsets
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, ProductoVariante
from catalogo.api.serializers import TipoProductoSerializer, CategoriaProductoSerializer, ColorSerializer, TallaSerializer, ProductoVarianteSerializer

class TipoProductoViewSet(viewsets.ModelViewSet):
    queryset = TipoProducto.objects.all()
    serializer_class = TipoProductoSerializer

class CategoriaProductoViewSet(viewsets.ModelViewSet):
    serializer_class = CategoriaProductoSerializer

    def get_queryset(self):
        return CategoriaProducto.objects.filter(activo=True)

class ColorViewSet(viewsets.ModelViewSet):
    serializer_class = ColorSerializer

    def get_queryset(self):
        return Color.objects.filter(activo=True)

class TallaViewSet(viewsets.ModelViewSet):
    serializer_class = TallaSerializer

    def get_queryset(self):
        return Talla.objects.filter(activo=True)
    
# TODO: Producto ViewSet
    
class ProductoVarianteViewSet(viewsets.ModelViewSet):
    serializer_class = ProductoVarianteSerializer

    def get_queryset(self):
        return ProductoVariante.objects.filter(activo=True)
