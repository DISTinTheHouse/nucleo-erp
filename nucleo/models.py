from django.db import models
from django.utils import timezone

class Status(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)

    class Meta:
        db_table = "status"
        verbose_name = "Status"
        verbose_name_plural = "Status"

    def __str__(self):
        return self.name

class SoftDeleteModel(models.Model):
    status = models.ForeignKey(Status, on_delete=models.PROTECT, default=1, related_name='soft_delete_models')

    class Meta:
        abstract = True

    def soft_delete(self):
        self.status = Status.objects.get(code='deleted')
        self.save(update_fields=['status'])

# =========================
# CATÁLOGOS BASE (globales)
# =========================

class Moneda(SoftDeleteModel):
    # Relación opcional con Empresa.
    # Si es NULL, es una moneda GLOBAL (del sistema).
    # Si tiene valor, es una moneda privada de esa empresa.
    empresa = models.ForeignKey('Empresa', on_delete=models.CASCADE, null=True, blank=True, related_name='monedas_privadas')
    
    codigo_iso = models.CharField(max_length=3)  # Quitamos unique=True global
    nombre = models.CharField(max_length=60)
    simbolo = models.CharField(max_length=10, blank=True, null=True)
    decimales = models.PositiveSmallIntegerField(default=2)
    estatus = models.BooleanField(default=True)
    status = models.ForeignKey(Status, on_delete=models.PROTECT, default=1, related_name='monedas')

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
    status = models.ForeignKey(Status, on_delete=models.PROTECT, default=1, related_name='impuestos')

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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='unidades_medida')

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

class Empresa(SoftDeleteModel):
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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='empresas')

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
        return self.codigo or f"Empresa {self.pk}"

class Sucursal(SoftDeleteModel):
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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='sucursales')

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

class Departamento(SoftDeleteModel):
    class Estatus(models.TextChoices):
        ACTIVO = "activo", "Activo"
        INACTIVO = "inactivo", "Inactivo"

    id_departamento = models.BigAutoField(primary_key=True)

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="departamentos")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="departamentos")

    codigo = models.CharField(max_length=50)  # <- obligatorio
    nombre = models.CharField(max_length=255)
    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='departamentos')

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
    
    def perform_destroy(self, instance):
        instance.soft_delete()

class SerieFolio(SoftDeleteModel):
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
    
    # Configuración de formato
    relleno_ceros = models.PositiveSmallIntegerField(default=6, help_text="Ceros a la izquierda. 0 para desactivar.")
    separador = models.CharField(max_length=5, default='-', blank=True, help_text="Caracter entre serie, folio y año")
    
    # Configuración de Año
    incluir_anio = models.BooleanField(default=False, help_text="Incluir el año al final del folio (ej: -24)")
    reiniciar_anual = models.BooleanField(default=False, help_text="Reiniciar el consecutivo al cambiar de año")
    ultimo_anio = models.PositiveSmallIntegerField(blank=True, null=True, help_text="Último año registrado (2 dígitos)")

    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='series_folios')

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

    def get_siguiente_folio(self):
        """
        Calcula el siguiente folio basado en la configuración.
        No guarda el incremento, solo retorna la cadena formateada y el número.
        """
        import datetime
        anio_actual = int(datetime.datetime.now().strftime('%y'))  # 24, 25, 26
        
        nuevo_consecutivo = self.folio_actual + 1

        # Lógica de reinicio anual
        if self.reiniciar_anual and self.ultimo_anio != anio_actual:
            nuevo_consecutivo = 1
        
        # Formateo del número
        if self.relleno_ceros > 0:
            numero_str = str(nuevo_consecutivo).zfill(self.relleno_ceros)
        else:
            numero_str = str(nuevo_consecutivo)

        # Construcción del folio completo
        partes = []
        if self.prefijo:
            partes.append(self.prefijo)
        
        partes.append(self.serie)
        partes.append(numero_str)
        
        if self.incluir_anio:
            partes.append(str(anio_actual))

        if self.sufijo:
            partes.append(self.sufijo)

        folio_formateado = self.separador.join(partes)
        
        # Corrección: si prefijo/sufijo no deben llevar separador, ajustar lógica.
        # Por simplicidad actual: Serie-Folio-Año (P-1-26)
        # Si se requiere P1-26, el separador debería ser vacio y manejar espacios en los campos.
        
        return folio_formateado, nuevo_consecutivo, anio_actual

    def incrementar_folio(self):
        """
        Incrementa el folio y actualiza el último año.
        """
        _, nuevo_consecutivo, anio_actual = self.get_siguiente_folio()
        self.folio_actual = nuevo_consecutivo
        self.ultimo_anio = anio_actual
        self.save()
        return self.folio_actual

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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='sat_uso_cfdi')

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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='sat_metodo_pago')

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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='sat_forma_pago')

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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='sat_clave_prodserv')

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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='sat_clave_unidad')

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
    status = models.ForeignKey(Status, on_delete=models.CASCADE, default=1, related_name='sat_regimen_fiscal')

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
