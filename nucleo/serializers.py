from rest_framework import serializers
from .models import Empresa, Moneda, Sucursal, Departamento, SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago, EmpresaSatConfig
from .utils import validate_csd, validate_rfc

class EmpresaSatConfigSerializer(serializers.ModelSerializer):
    archivo_cer = serializers.FileField(write_only=True, required=False)
    archivo_key = serializers.FileField(write_only=True, required=False)
    
    class Meta:
        model = EmpresaSatConfig
        fields = [
            'id_empresa_sat_config', 'empresa', 'regimen_fiscal',
            'archivo_cer', 'archivo_key', 'password_llave',
            'no_certificado', 'fecha_expiracion', 'validado', 'mensaje_error'
        ]
        read_only_fields = ['validado', 'mensaje_error', 'no_certificado', 'fecha_expiracion']

    def update(self, instance, validated_data):
        cer_file = validated_data.get('archivo_cer')
        key_file = validated_data.get('archivo_key')
        password = validated_data.get('password_llave', instance.password_llave)

        # Actualizar campos normales
        for attr, value in validated_data.items():
            if attr not in ['archivo_cer', 'archivo_key']:
                setattr(instance, attr, value)

        # Si se suben archivos, validar
        if cer_file and key_file:
            try:
                cer_content = cer_file.read()
                key_content = key_file.read()
                
                # Reiniciar punteros para guardar
                cer_file.seek(0)
                key_file.seek(0)
                
                is_valid, result = validate_csd(cer_content, key_content, password)
                
                if is_valid:
                    instance.validado = True
                    instance.mensaje_error = None
                    instance.no_certificado = result['no_certificado']
                    instance.fecha_expiracion = result['fecha_expiracion']
                    
                    # Validar RFC vs Empresa si es posible
                    if instance.empresa.rfc and result['rfc']:
                         # Limpieza básica para comparar
                         rfc_cert = result['rfc'].strip().upper()
                         rfc_empresa = instance.empresa.rfc.strip().upper()
                         if rfc_cert != rfc_empresa:
                             instance.validado = False
                             instance.mensaje_error = f"RFC incorrecto. Certificado: {rfc_cert}, Empresa: {rfc_empresa}"
                    
                    instance.archivo_cer = cer_file
                    instance.archivo_key = key_file
                else:
                    instance.validado = False
                    instance.mensaje_error = result
                    # Guardamos archivos aunque sean inválidos para revisión, o rechazamos?
                    # Mejor guardamos para que el usuario vea que se subieron pero fallaron
                    instance.archivo_cer = cer_file
                    instance.archivo_key = key_file
            except Exception as e:
                instance.validado = False
                instance.mensaje_error = f"Error procesando archivos: {str(e)}"
        
        elif cer_file or key_file:
             # Si sube solo uno, invalidamos porque falta el par
             instance.validado = False
             instance.mensaje_error = "Debe subir ambos archivos (.cer y .key) y la contraseña para validar."
             if cer_file: instance.archivo_cer = cer_file
             if key_file: instance.archivo_key = key_file

        instance.save()
        return instance

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

class MonedaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Moneda
        fields = ['id', 'empresa', 'codigo_iso', 'nombre', 'simbolo', 'decimales', 'estatus']
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
            'moneda_base', 'timezone', 'idioma', 'estatus', 'logo_url'
        ]
        read_only_fields = ['id_empresa', 'created_at', 'updated_at', 'deleted_at']

    def validate_rfc(self, value):
        if not value:
            return value
        is_valid, error_msg = validate_rfc(value)
        if not is_valid:
            raise serializers.ValidationError(error_msg)
        return value.upper()

