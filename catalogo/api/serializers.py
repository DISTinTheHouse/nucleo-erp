from rest_framework import serializers
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante

class TipoProductoSerializer(serializers.ModelSerializer):
    class Meta:
        model = TipoProducto
        fields = '__all__'

class CategoriaProductoSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoriaProducto
        exclude = ['activo', 'created_at', 'updated_at']

class ColorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Color
        exclude = ['activo']

class TallaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Talla
        exclude = ['activo']

class ProductoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Producto
        fields = '__all__'
        extra_kwargs = {
            'empresa': {'required': False},
        }

class ProductoOnboardingSerializer(serializers.ModelSerializer):
    """Alta simplificada de Producto para producción: nombre + tipo + categoria + precio, sin descripcion."""
    tipo = serializers.PrimaryKeyRelatedField(queryset=TipoProducto.objects.all())
    categoria_producto = serializers.PrimaryKeyRelatedField(queryset=CategoriaProducto.objects.all())
    precio_base = serializers.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        model = Producto
        fields = ['id', 'nombre', 'tipo', 'categoria_producto', 'precio_base']

class ProductoVarianteOnboardingSerializer(serializers.ModelSerializer):
    """Alta simplificada de ProductoVariante: producto + color + talla + precio, el SKU se calcula en el server."""
    producto = serializers.PrimaryKeyRelatedField(queryset=Producto.objects.all())
    color = serializers.PrimaryKeyRelatedField(queryset=Color.objects.all())
    talla = serializers.PrimaryKeyRelatedField(queryset=Talla.objects.all())
    precio_base = serializers.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        model = ProductoVariante
        fields = ['id', 'producto', 'color', 'talla', 'precio_base', 'sku']
        read_only_fields = ['sku']

class ProductoVarianteSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    cod_proscai = serializers.CharField(source='producto.cod_proscai', read_only=True)

    class Meta:
        model = ProductoVariante
        fields = '__all__'
