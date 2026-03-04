from produccion.models import ListaMaterialBom, OrdenProduccion, ConsumoProduccion, ProductoTerminadoEntradas
from rest_framework import serializers

class ListaMaterialBomSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListaMaterialBom
        fields = '__all__'

class OrdenProduccionSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenProduccion
        fields = '__all__'

class ConsumoProduccionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConsumoProduccion
        fields = '__all__'

class ProductoTerminadoEntradasSerializer(serializers.ModelSerializer):
    class Meta: 
        model = ProductoTerminadoEntradas
        fields = '__all__'
