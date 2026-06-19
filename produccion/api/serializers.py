from produccion.models import (
    ListaMaterialBom,
    BomDetalle,
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

class BomDetalleSerializer(serializers.ModelSerializer):
    componente_nombre = serializers.SerializerMethodField()
    unidad_clave = serializers.SerializerMethodField()

    def get_componente_nombre(self, obj):
        return obj.componente.nombre if obj.componente else None

    def get_unidad_clave(self, obj):
        return obj.unidad.clave if obj.unidad else None

    class Meta:
        model = BomDetalle
        fields = '__all__'
        read_only_fields = ['bom', 'activo']

class ListaMaterialBomSerializer(serializers.ModelSerializer):
    materia_prima_detalle = BomDetalleSerializer(many=True)

    class Meta:
        model = ListaMaterialBom
        fields = '__all__'
        read_only_fields = ['activo', 'bom_id']
    
    def create(self, validated_data):
        detallles_data = validated_data.pop('materia_prima_detalle')

        try:
            bom = ListaMaterialBom.objects.create(**validated_data)
            detalles = [
                BomDetalle(bom=bom, **detalle)
                for detalle in detallles_data
            ]
            BomDetalle.objects.bulk_create(detalles)
            
            return bom
        except Exception as e:
            raise serializers.ValidationError("Error creating bom")

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