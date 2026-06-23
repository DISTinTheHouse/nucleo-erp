from rest_framework import serializers

from produccion.models import (
    ListaMaterialBom,
    BomDetalle,
    OrdenProduccion,
    OrdenProduccionDetalle,
    ConsumoProduccion, 
    ProductoTerminadoEntradas, 
    OrdenesBordado,
    BordadoAvances,
    BordadoIncidencias,
    OrdenesReflejante,
    ReflejanteAvances,
    ReflejanteIncidencias
)

from catalogo.api.serializers import ProductoVarianteSerializer
from catalogo.models import ProductoVariante


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

class OrdenProduccionDetalleSerializer(serializers.ModelSerializer):
    producto_variante = ProductoVarianteSerializer(read_only=True)
    producto_variante_id = serializers.PrimaryKeyRelatedField(
        queryset=ProductoVariante.objects.all(),
        source='producto_variante',
        write_only=True
    )
    bom_detalle = BomDetalleSerializer(
        source='bom.materia_prima_detalle',
        many=True,
        read_only=True
    )

    class Meta:
        model = OrdenProduccionDetalle
        fields = '__all__'
        read_only_fields = ['activo', 'op']

class OrdenProduccionSerializer(serializers.ModelSerializer):
    orden_produccion_detalle = OrdenProduccionDetalleSerializer(many=True)
    
    class Meta:
        model = OrdenProduccion
        fields = '__all__'
        read_only_fields = ['folio_op', 'activo', 'usuario_asignado']

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