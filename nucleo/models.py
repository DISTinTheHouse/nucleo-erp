from django.db import models
from django.utils import timezone


# =========================
# CATÁLOGOS BASE (globales)
# =========================

class Moneda(models.Model):
    # Relación opcional con Empresa.
    # Si es NULL, es una moneda GLOBAL (del sistema).
    # Si tiene valor, es una moneda privada de esa empresa.
    empresa = models.ForeignKey('Empresa', on_delete=models.CASCADE, null=True, blank=True, related_name='monedas_privadas')
    
    codigo_iso = models.CharField(max_length=3)  # Quitamos unique=True global
    nombre = models.CharField(max_length=60)
    simbolo = models.CharField(max_length=10, blank=True, null=True)
    decimales = models.PositiveSmallIntegerField(default=2)
    estatus = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "monedas"
        verbose_name = "Moneda"
        verbose_name_plural = "Monedas"
        ordering = ["codigo_iso"]
        constraints = [
            # Evitar duplicados dentro de la misma empresa
            models.UniqueConstraint(fields=['empresa', 'codigo_iso'], name='unique_moneda_empresa'),
            # Evitar duplicados en globales (empresa=null)
            # Nota: En algunos DBs (Postgres), null!=null, por lo que esto requiere validación en serializer o índice parcial.
            # Para simplificar y compatibilidad, lo manejaremos fuertemente en Serializer/Clean.
        ]

    def __str__(self):
        return self.codigo_iso

class Impuesto(models.Model):
    class Tipo(models.TextChoices):
        TRASLADADO = "trasladado", "Trasladado"
        RETENCION = "retencion", "Retención"

    codigo = models.CharField(max_length=20, unique=True)  # IVA16, ISR10, etc.
    nombre = models.CharField(max_length=100)
    tasa = models.DecimalField(max_digits=8, decimal_places=6, default=0)  # 0.160000
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.TRASLADADO)
    estatus = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "impuestos"
        verbose_name = "Impuesto"
        verbose_name_plural = "Impuestos"
        ordering = ["codigo"]

    def __str__(self):
        return f"{self.codigo}"

class UnidadMedida(models.Model):
    clave = models.CharField(max_length=10, unique=True)  # PZA, MTR, KG
    nombre = models.CharField(max_length=100)
    estatus = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "unidades_medida"
        verbose_name = "Unidad de Medida"
        verbose_name_plural = "Unidades de Medida"
        ordering = ["clave"]

    def __str__(self):
        return self.clave

# =========================
# 0) NÚCLEO / MULTI-TENANT
# =========================

class Empresa(models.Model):
    class Estatus(models.TextChoices):
        ACTIVO = "activo", "Activo"
        SUSPENDIDO = "suspendido", "Suspendido"

    id_empresa = models.BigAutoField(primary_key=True)

    codigo = models.SlugField(max_length=32, unique=True)  # tenant key para URLs/prefijos
    razon_social = models.CharField(max_length=255)
    nombre_comercial = models.CharField(max_length=255, blank=True, null=True)
    rfc = models.CharField(max_length=13, blank=True, null=True)

    email_contacto = models.EmailField(blank=True, null=True)
    telefono = models.CharField(max_length=30, blank=True, null=True)
    sitio_web = models.URLField(blank=True, null=True)

    moneda_base = models.ForeignKey(
        Moneda, on_delete=models.PROTECT, related_name="empresas", blank=True, null=True
    )
    timezone = models.CharField(max_length=64, default="America/Mexico_City")
    idioma = models.CharField(max_length=16, default="es-MX")

    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)

    logo_url = models.URLField(blank=True, null=True)
    config_json = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "empresas"
        verbose_name = "Empresa"
        verbose_name_plural = "Empresas"
        indexes = [
            models.Index(fields=["codigo"]),
            models.Index(fields=["estatus"]),
        ]

    def __str__(self):
        return self.codigo

    def soft_delete(self):
        self.deleted_at = timezone.now()
        self.estatus = self.Estatus.SUSPENDIDO
        self.save(update_fields=["deleted_at", "estatus", "updated_at"])


class Sucursal(models.Model):
    class Estatus(models.TextChoices):
        ACTIVO = "activo", "Activo"
        INACTIVO = "inactivo", "Inactivo"

    id_sucursal = models.BigAutoField(primary_key=True)

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="sucursales")
    codigo = models.CharField(max_length=50)  # unique por empresa
    nombre = models.CharField(max_length=255)

    telefono = models.CharField(max_length=30, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)

    direccion_linea1 = models.CharField(max_length=255, blank=True, null=True)
    direccion_linea2 = models.CharField(max_length=255, blank=True, null=True)
    ciudad = models.CharField(max_length=100, blank=True, null=True)
    estado = models.CharField(max_length=100, blank=True, null=True)
    cp = models.CharField(max_length=20, blank=True, null=True)
    pais = models.CharField(max_length=100, blank=True, null=True)

    lat = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)
    lng = models.DecimalField(max_digits=10, decimal_places=7, blank=True, null=True)

    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sucursales"
        verbose_name = "Sucursal"
        verbose_name_plural = "Sucursales"
        constraints = [
            models.UniqueConstraint(fields=["empresa", "codigo"], name="uq_sucursal_empresa_codigo"),
            models.UniqueConstraint(fields=["empresa", "nombre"], name="uq_sucursal_empresa_nombre"),
        ]
        indexes = [
            models.Index(fields=["empresa", "codigo"]),
            models.Index(fields=["empresa", "estatus"]),
        ]

    def __str__(self):
        return f"{self.empresa.codigo} - {self.nombre}"


class Departamento(models.Model):
    class Estatus(models.TextChoices):
        ACTIVO = "activo", "Activo"
        INACTIVO = "inactivo", "Inactivo"

    id_departamento = models.BigAutoField(primary_key=True)

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="departamentos")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="departamentos")

    codigo = models.CharField(max_length=50)  # <- obligatorio
    nombre = models.CharField(max_length=255)
    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "departamentos"
        verbose_name = "Departamento"
        verbose_name_plural = "Departamentos"
        constraints = [
            models.UniqueConstraint(fields=["sucursal", "codigo"], name="uq_departamento_sucursal_codigo"),
            # opcional recomendado:
            # models.UniqueConstraint(fields=["sucursal", "nombre"], name="uq_departamento_sucursal_nombre"),
        ]
        indexes = [
            models.Index(fields=["empresa", "estatus"]),
        ]

    def __str__(self):
        return f"{self.sucursal.nombre} - {self.nombre}"


class SerieFolio(models.Model):
    class Estatus(models.TextChoices):
        ACTIVO = "activo", "Activo"
        INACTIVO = "inactivo", "Inactivo"

    id_serie_folio = models.BigAutoField(primary_key=True)

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="series_folios")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="series_folios")

    tipo_documento = models.CharField(max_length=50)  # PEDIDO, FACTURA, OC, OP, etc.
    serie = models.CharField(max_length=20)
    folio_actual = models.PositiveIntegerField(default=0)

    folio_inicial = models.PositiveIntegerField(blank=True, null=True)
    folio_final = models.PositiveIntegerField(blank=True, null=True)

    prefijo = models.CharField(max_length=20, blank=True, null=True)
    sufijo = models.CharField(max_length=20, blank=True, null=True)
    relleno_ceros = models.PositiveSmallIntegerField(default=6)

    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "series_folios"
        verbose_name = "Serie/Folio"
        verbose_name_plural = "Series/Folios"
        constraints = [
            models.UniqueConstraint(
                fields=["sucursal", "tipo_documento", "serie"],
                name="uq_serie_folio_sucursal_tipo_serie",
            ),
        ]
        indexes = [
            models.Index(fields=["empresa", "tipo_documento"]),
            models.Index(fields=["sucursal", "tipo_documento"]),
        ]

    def __str__(self):
        return f"{self.tipo_documento} {self.serie}"


# =========================
# CATÁLOGOS SAT (Globales)
# =========================

class SatUsoCfdi(models.Model):
    id_sat_uso_cfdi = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=10, unique=True)  # G01, G03
    descripcion = models.CharField(max_length=255)
    aplica_fisica = models.BooleanField(default=False)
    aplica_moral = models.BooleanField(default=False)
    estatus = models.CharField(max_length=20, default='activo')

    class Meta:
        db_table = "sat_uso_cfdi"
        verbose_name = "SAT Uso CFDI"
        verbose_name_plural = "SAT Usos CFDI"

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"


class SatMetodoPago(models.Model):
    id_sat_metodo_pago = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=10, unique=True)
    descripcion = models.CharField(max_length=255)
    estatus = models.CharField(max_length=20, default='activo')

    class Meta:
        db_table = "sat_metodo_pago"
        verbose_name = "SAT Método de Pago"
        verbose_name_plural = "SAT Métodos de Pago"

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"


class SatFormaPago(models.Model):
    id_sat_forma_pago = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=10, unique=True)
    descripcion = models.CharField(max_length=255)
    bancarizado = models.BooleanField(default=False)
    estatus = models.CharField(max_length=20, default='activo')

    class Meta:
        db_table = "sat_forma_pago"
        verbose_name = "SAT Forma de Pago"
        verbose_name_plural = "SAT Formas de Pago"

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"


class SatClaveProdServ(models.Model):
    id_sat_prodserv = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=20, unique=True)
    descripcion = models.CharField(max_length=255)
    estatus = models.CharField(max_length=20, default='activo')

    class Meta:
        db_table = "sat_clave_prodserv"
        verbose_name = "SAT Clave Prod/Serv"
        verbose_name_plural = "SAT Claves Prod/Serv"

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"


class SatClaveUnidad(models.Model):
    id_sat_unidad = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=10, unique=True)
    descripcion = models.CharField(max_length=255)
    estatus = models.CharField(max_length=20, default='activo')

    class Meta:
        db_table = "sat_clave_unidad"
        verbose_name = "SAT Clave Unidad"
        verbose_name_plural = "SAT Claves Unidad"

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"


class SatRegimenFiscal(models.Model):
    id_sat_regimen_fiscal = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=10, unique=True)  # 601, 626...
    descripcion = models.CharField(max_length=255)
    aplica_fisica = models.BooleanField(default=False)
    aplica_moral = models.BooleanField(default=False)
    estatus = models.CharField(max_length=20, default='activo')

    class Meta:
        db_table = "sat_regimen_fiscal"
        verbose_name = "SAT Régimen Fiscal"
        verbose_name_plural = "SAT Regímenes Fiscales"

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"


# =========================
# CONFIGURACIÓN FISCAL
# =========================

def get_upload_path_cer(instance, filename):
    return f"sat/csd/{instance.empresa.codigo}/cer_{filename}"

def get_upload_path_key(instance, filename):
    return f"sat/csd/{instance.empresa.codigo}/key_{filename}"


class EmpresaSatConfig(models.Model):
    id_empresa_sat_config = models.BigAutoField(primary_key=True)
    empresa = models.OneToOneField(Empresa, on_delete=models.CASCADE, related_name="sat_config")
    regimen_fiscal = models.ForeignKey(SatRegimenFiscal, on_delete=models.PROTECT, related_name="empresas_config", blank=True, null=True)

    # Archivos físicos
    archivo_cer = models.FileField(upload_to=get_upload_path_cer, blank=True, null=True, help_text="Certificado (.cer)")
    archivo_key = models.FileField(upload_to=get_upload_path_key, blank=True, null=True, help_text="Llave Privada (.key)")
    
    # Contenido en Base64/PEM para uso interno (se llena automático)
    certificado_pem = models.TextField(blank=True, null=True)
    llave_privada_pem = models.TextField(blank=True, null=True)
    
    password_llave = models.CharField(max_length=255, blank=True, null=True)
    no_certificado = models.CharField(max_length=50, blank=True, null=True)
    fecha_expiracion = models.DateTimeField(blank=True, null=True)
    
    validado = models.BooleanField(default=False)
    mensaje_error = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "empresa_sat_config"
        verbose_name = "Configuración SAT Empresa"
        verbose_name_plural = "Configuraciones SAT Empresa"

    def __str__(self):
        return f"Config SAT - {self.empresa.razon_social}"
