from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante
from catalogo.api.serializers import TipoProductoSerializer, CategoriaProductoSerializer, ColorSerializer, TallaSerializer, ProductoSerializer, ProductoVarianteSerializer

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
    
class ProductoViewSet(viewsets.ModelViewSet):
    queryset = Producto.objects.all()
    serializer_class = ProductoSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        tipo_id = self.request.query_params.get('tipo_id')
        if tipo_id is not None:
            try:
                tipo_id = int(tipo_id)
            except (TypeError, ValueError):
                raise ValidationError({"tipo_id": "Must be an integer."})
            qs = qs.filter(tipo_id=tipo_id)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        if not getattr(user, "is_superuser", False) and empresa:
            serializer.save(empresa=empresa)
            return
        serializer.save()
    
class ProductoVarianteViewSet(viewsets.ModelViewSet):
    queryset = ProductoVariante.objects.all()
    serializer_class = ProductoVarianteSerializer


