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
        fields = '__all__'

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

class TenantRegistrationSerializer(serializers.Serializer):
    """
    Serializer para el flujo de onboarding:
    1. Crear Empresa
    2. Crear Sucursal (Matriz)
    3. Crear Usuario (Admin)
    """
    # Empresa
    empresa_razon_social = serializers.CharField(max_length=255)
    empresa_codigo = serializers.SlugField(max_length=32)
    empresa_rfc = serializers.CharField(max_length=13, required=False, allow_blank=True)
    empresa_email = serializers.EmailField(required=False, allow_blank=True)
    
    # Sucursal
    sucursal_nombre = serializers.CharField(max_length=255)
    sucursal_codigo = serializers.CharField(max_length=50)
    
    # Usuario
    usuario_username = serializers.CharField(max_length=150)
    usuario_email = serializers.EmailField()
    usuario_password = serializers.CharField(write_only=True, style={'input_type': 'password'})
    usuario_first_name = serializers.CharField(max_length=150)
    usuario_last_name = serializers.CharField(max_length=150)

    def validate_empresa_codigo(self, value):
        if Empresa.objects.filter(codigo=value).exists():
            raise serializers.ValidationError("Este código de empresa ya está registrado.")
        return value

    def validate_empresa_rfc(self, value):
        if not value:
            return value
        is_valid, error_msg = validate_rfc(value)
        if not is_valid:
            raise serializers.ValidationError(error_msg)
        return value.upper()

    def validate_usuario_username(self, value):
        from usuarios.models import Usuario
        if Usuario.objects.filter(username=value).exists():
            raise serializers.ValidationError("Este nombre de usuario ya está en uso.")
        return value
    
    def validate_usuario_email(self, value):
        from usuarios.models import Usuario
        if Usuario.objects.filter(email=value).exists():
            raise serializers.ValidationError("Este correo electrónico ya está registrado.")
        return value

    def create(self, validated_data):
        from django.db import transaction
        from usuarios.models import Usuario

        with transaction.atomic():
            # 1. Crear Empresa
            empresa = Empresa.objects.create(
                codigo=validated_data['empresa_codigo'],
                razon_social=validated_data['empresa_razon_social'],
                rfc=validated_data.get('empresa_rfc', '').upper() if validated_data.get('empresa_rfc') else None,
                email_contacto=validated_data.get('empresa_email'),
                estatus=Empresa.Estatus.ACTIVO
            )

            # 2. Crear Sucursal
            sucursal = Sucursal.objects.create(
                empresa=empresa,
                codigo=validated_data['sucursal_codigo'],
                nombre=validated_data['sucursal_nombre'],
                estatus=Sucursal.Estatus.ACTIVO
            )

            # 3. Crear Usuario
            usuario = Usuario.objects.create_user(
                username=validated_data['usuario_username'],
                email=validated_data['usuario_email'],
                password=validated_data['usuario_password'],
                first_name=validated_data['usuario_first_name'],
                last_name=validated_data['usuario_last_name'],
                empresa=empresa,
                sucursal_default=sucursal,
                is_admin_empresa=True, # Rol Admin Empresa
                estatus=Usuario.Estatus.ACTIVO
            )
            
            # Asignar sucursal permitida
            usuario.sucursales.add(sucursal)
            
            # (Opcional) Si existieran roles default, asignarlos aquí
            
            return {
                'empresa': empresa,
                'sucursal': sucursal,
                'usuario': usuario
            }
