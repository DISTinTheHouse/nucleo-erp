from rest_framework import serializers
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
from django.db import transaction
from nucleo.models import SerieFolio

class OrdenCompraSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenCompra
        fields = '__all__'
        extra_kwargs = {
            'folio': {'required': False, 'allow_null': True},
            'solicitud_compra': {'required': False, 'allow_null': True},
            'pedido': {'required': False, 'allow_null': True},
            'proveedor': {'required': False, 'allow_null': True},
        }

    def to_internal_value(self, data):
        # Convert "0" or 0 to None for FK fields if they are optional
        for field in ['solicitud_compra', 'pedido', 'proveedor']:
            if field in data and (data[field] == "0" or data[field] == 0):
                data[field] = None
        return super().to_internal_value(data)


class OrdenCompraDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrdenCompraDetalle
        fields = "__all__"


class OrdenCompraOnboardingHeaderSerializer(serializers.Serializer):
    sucursal = serializers.IntegerField(required=False, allow_null=True)
    proveedor = serializers.IntegerField(required=False, allow_null=True)
    moneda = serializers.IntegerField(required=False, allow_null=True)
    fecha_oc = serializers.DateField(required=False, allow_null=True)
    referencia = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    observaciones = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class OrdenCompraOnboardingDetalleInputSerializer(serializers.Serializer):
    producto = serializers.IntegerField()
    cantidad = serializers.IntegerField(min_value=1)
    precio = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    descripcion = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class OrdenCompraOnboardingSerializer(serializers.Serializer):
    orden_compra_id = serializers.IntegerField(required=False)
    orden_compra = OrdenCompraOnboardingHeaderSerializer(required=False)
    detalle = OrdenCompraOnboardingDetalleInputSerializer(many=True, required=False)

class RecepcionDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecepcionDetalle
        fields = '__all__'
        read_only_fields = ('recepcion',)

class RecepcionSerializer(serializers.ModelSerializer):
    detail = RecepcionDetalleSerializer(write_only=True)
    
    class Meta:
        model = Recepcion
        fields = '__all__'
        read_only_fields = ('folio',)
    
    @transaction.atomic
    def create(self, validated_data):
        detail_data = validated_data.pop('detail')
        
        empresa = validated_data.get('empresa')
        sucursal = validated_data.get('sucursal')
        
        serie_folio = SerieFolio.objects.select_for_update().filter(
            empresa=empresa,
            sucursal=sucursal,
            tipo_documento__iexact="Recepcion",
            activo=True
        ).order_by("id_serie_folio").first()
        
        if not serie_folio:
            raise serializers.ValidationError(
                {"serie_folio": "No hay una Serie/Folio activa configurada para tipo_documento='Recepcion' en esta sucursal."}
            )
            
        try:
            folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
        except Exception as e:
            raise serializers.ValidationError(
                {"folio": "No se pudo generar el folio de la recepción."}
            )
            
        serie_folio.folio_actual = nuevo_consecutivo
        serie_folio.ultimo_anio = anio_actual
        serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])
        
        validated_data['folio'] = folio_formateado
        
        recepcion = Recepcion.objects.create(**validated_data)
        RecepcionDetalle.objects.create(recepcion=recepcion, **detail_data)
        return recepcion


        

