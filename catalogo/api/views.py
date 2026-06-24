from django.db.models import Exists, OuterRef
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante
from catalogo.api.serializers import TipoProductoSerializer, CategoriaProductoSerializer, ColorSerializer, TallaSerializer, ProductoSerializer, ProductoVarianteSerializer
from produccion.models import ListaMaterialBom

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
    # producto_nombre/color_nombre/talla_nombre (agregados en 0599352) recorren las FK
    # producto, color y talla por su atributo .nombre. Con un queryset .all() sin
    # select_related, DRF resuelve esas relaciones de forma perezosa fila por fila: el
    # listado completo dispara 3 consultas por variante (N+1) y sobre la tabla real de
    # variantes el endpoint tarda tanto que parece colgarse. select_related las trae en el
    # mismo JOIN -> una sola consulta, sin alterar la forma de la respuesta.
    queryset = ProductoVariante.objects.all().select_related("producto", "color", "talla")
    serializer_class = ProductoVarianteSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get('con_bom', '').lower() == 'true':
            bom_qs = ListaMaterialBom.objects.filter(
                producto_variante=OuterRef('pk'),
                activo=True,
            )
            empresa = getattr(self.request.user, 'empresa', None)
            if empresa:
                bom_qs = bom_qs.filter(empresa=empresa)
            qs = qs.filter(Exists(bom_qs))
        return qs


