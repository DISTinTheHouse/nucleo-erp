from rest_framework import serializers
from ..models import (
    Empresa, Moneda, Sucursal, Departamento, 
    SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago, 
    SatClaveProdServ, SatClaveUnidad,
    SerieFolio,
    Impuesto, UnidadMedida
)
from ..utils import validate_rfc

class SatRegimenFiscalSerializer(serializers.ModelSerializer):
    class Meta:
        model = SatRegimenFiscal
        fields = ['id_sat_regimen_fiscal', 'codigo', 'descripcion', 'aplica_fisica', 'aplica_moral']

class SatUsoCfdiSerializer(serializers.ModelSerializer):
    class Meta:
        model = SatUsoCfdi
        fields = ['id_sat_uso_cfdi', 'codigo', 'descripcion', 'aplica_fisica', 'aplica_moral']

class SatMetodoPagoSerializer(serializers.ModelSerializer):
    class Meta:
        model = SatMetodoPago
        fields = ['id_sat_metodo_pago', 'codigo', 'descripcion']

class SatFormaPagoSerializer(serializers.ModelSerializer):
    class Meta:
        model = SatFormaPago
        fields = ['id_sat_forma_pago', 'codigo', 'descripcion', 'bancarizado']

class SatClaveProdServSerializer(serializers.ModelSerializer):
    class Meta:
        model = SatClaveProdServ
        fields = ['id_sat_prodserv', 'codigo', 'descripcion', 'activo']

class SatClaveUnidadSerializer(serializers.ModelSerializer):
    class Meta:
        model = SatClaveUnidad
        fields = ['id_sat_unidad', 'codigo', 'descripcion', 'activo']

class UnidadMedidaSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnidadMedida
        fields = ['id', 'clave', 'nombre', 'activo']

class ImpuestoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Impuesto
        fields = ['id', 'codigo', 'nombre', 'tasa', 'tipo', 'activo']

class MonedaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Moneda
        fields = ['id', 'empresa', 'codigo_iso', 'nombre', 'simbolo', 'decimales', 'activo']
        read_only_fields = ['id', 'empresa'] # Empresa se asigna en el viewset

    def validate(self, data):
        request = self.context.get('request')
        if not request or not request.user:
            return data
            
        codigo_iso = data.get('codigo_iso')
        if not codigo_iso:
            return data # Dejar que la validación estándar de required lo maneje
            
        user = request.user
        empresa = user.empresa if not user.is_superuser else None
        
        # Validar unicidad (Global o Local)
        # 1. Si existe como global
        if Moneda.objects.filter(codigo_iso=codigo_iso, empresa__isnull=True).exists():
             # Si el usuario NO es superuser, no puede crear una que ya existe globalmente (se usa la global)
             if not user.is_superuser:
                 raise serializers.ValidationError(f"La moneda {codigo_iso} ya existe en el catálogo global.")
             
             # Si es superuser y está intentando crear otra global igual
             if user.is_superuser and not data.get('empresa'):
                 # Chequear si estamos actualizando la misma instancia
                 if self.instance and self.instance.codigo_iso == codigo_iso:
                     pass
                 else:
                     raise serializers.ValidationError(f"La moneda global {codigo_iso} ya existe.")

        # 2. Si existe localmente para esta empresa
        if empresa:
             qs = Moneda.objects.filter(codigo_iso=codigo_iso, empresa=empresa)
             if self.instance:
                 qs = qs.exclude(pk=self.instance.pk)
             if qs.exists():
                 raise serializers.ValidationError(f"Ya tienes configurada la moneda {codigo_iso} en tu empresa.")
                 
        return data

class SucursalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sucursal
        fields = '__all__'

class DepartamentoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Departamento
        fields = '__all__'

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
            'moneda_base', 'timezone', 'idioma', 'activo', 'logo_url'
        ]
        read_only_fields = ['id_empresa', 'created_at', 'updated_at', 'deleted_at']

    def validate_rfc(self, value):
        if not value:
            return value
        is_valid, error_msg = validate_rfc(value)
        if not is_valid:
            raise serializers.ValidationError(error_msg)
        return value.upper()

class SerieFolioSerializer(serializers.ModelSerializer):
    class Meta:
        model = SerieFolio
        fields = '__all__'
        read_only_fields = ['id_serie_folio', 'empresa', 'folio_actual', 'ultimo_anio', 'created_at', 'updated_at']

    def validate(self, attrs):
        # Validar que la sucursal pertenezca a la empresa
        # Nota: 'empresa' no viene en attrs porque es read_only, se obtiene del usuario en perform_create
        # Pero para validación aquí, necesitamos saber la empresa del contexto o de la instancia.
        
        request = self.context.get('request')
        user = request.user if request else None
        
        empresa = None
        if self.instance:
            empresa = self.instance.empresa
        elif user and not user.is_superuser and user.empresa:
            empresa = user.empresa
            
        # Si es superuser y no hay instancia, podría faltar empresa si no se pasa... 
        # pero como es read_only, el superuser tampoco podría pasarla por el body directamente si usamos el mismo serializer.
        # Asumimos flujo estándar de usuario normal por ahora.

        sucursal = attrs.get('sucursal')
        
        # Si es update, obtener valores de la instancia si no vienen en attrs
        if self.instance:
            sucursal = sucursal or self.instance.sucursal

        if empresa and sucursal and sucursal.empresa != empresa:
             raise serializers.ValidationError({'sucursal': 'La sucursal no pertenece a la empresa seleccionada.'})
        
        return attrs

