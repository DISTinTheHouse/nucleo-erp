from rest_framework import serializers
from wms.models import Transferencia, TransferenciaDetalle

class TransferenciaDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransferenciaDetalle
        fields = "__all__"
        read_only_fields = ["transferencia"]
    
    def validate(self, attrs):
        producto = attrs.get('producto')
        producto_variante = attrs.get('producto_variante')

        if not producto and not producto_variante:
            raise serializers.ValidationError({
                "non_field_errors": [
                    "Debe proporcionar 'producto' o 'producto_variante'."
                ]
            })
        
        if producto and producto_variante:
            raise serializers.ValidationError({
                "non_field_errors": [
                    "Solo puede proporcionar 'producto' o 'producto_variante'."
                ]
            })
        
        return attrs

class TransferenciaSerializer(serializers.ModelSerializer):
    transferencia_detalle = TransferenciaDetalleSerializer(many=True)

    class Meta:
        model = Transferencia
        fields = "__all__"
        read_only_fields = ["empresa", "sucursal", "folio", "usuario", "status"]

    def validate(self, attrs):
        almacen_origen = attrs.get('almacen_origen')
        almacen_destino = attrs.get('almacen_destino')

        if almacen_origen == almacen_destino:
            raise serializers.ValidationError({
                "non_field_errors": [
                    "El almacen de origen y destino no pueden ser iguales."
                ]
            })
        
        return attrs