from produccion.models import (
    ListaMaterialBom, 
    OrdenProduccion, 
    ConsumoProduccion, 
    ProductoTerminadoEntradas, 
    OrdenesBordado,
    BordadoAvances,
    BordadoIncidencias,
    OrdenesReflejante,
    ReflejanteAvances,
    ReflejanteIncidencias
)

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

class OrdenBordadoSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenesBordado
        fields = '__all__'

class BordadoAvancesSerializer(serializers.ModelSerializer):
    class Meta:
        model = BordadoAvances
        fields = '__all__'
        
class BordadoIncidenciasSerializer(serializers.ModelSerializer):
    class Meta:
        model = BordadoIncidencias
        fields = '__all__'

class OrdenReflejanteSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenesReflejante
        fields = '__all__'
        
class ReflejanteAvancesSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReflejanteAvances
        fields = '__all__'
        
class ReflejanteIncidenciasSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReflejanteIncidencias
        fields = '__all__'