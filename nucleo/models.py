from django.db import models
from django.utils import timezone


# =========================
# CATÁLOGOS BASE (globales)
# =========================

class Moneda(models.Model):
    codigo_iso = models.CharField(max_length=3, unique=True)  # MXN, USD
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
            models.Index(fields=["empresa"]),
            models.Index(fields=["sucursal"]),
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
