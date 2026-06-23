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

class ProductoVarianteSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)

    class Meta:
        model = ProductoVariante
        fields = '__all__'
