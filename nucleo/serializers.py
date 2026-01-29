from rest_framework import serializers
from .models import Empresa, Moneda

class EmpresaSerializer(serializers.ModelSerializer):
    # Permitir buscar/asignar moneda por su código ISO (ej: 'MXN') en lugar de ID
    moneda_base = serializers.SlugRelatedField(
        slug_field='codigo_iso',
        queryset=Moneda.objects.all(),
        required=False
    )

    class Meta:
        model = Empresa
        fields = [
            'id_empresa', 'codigo', 'razon_social', 'nombre_comercial', 
            'rfc', 'email_contacto', 'telefono', 'sitio_web', 
            'moneda_base', 'timezone', 'idioma', 'estatus', 'logo_url'
        ]
        read_only_fields = ['id_empresa', 'created_at', 'updated_at', 'deleted_at']
