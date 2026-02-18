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

#TODO: Producto serializer

class ProductoVarianteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductoVariante
        fields = '__all__'
