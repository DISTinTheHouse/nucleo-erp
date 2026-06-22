from django.db import models
from django.conf import settings
from nucleo.models import StatusLifecycleModel
from nucleo.models import Empresa, SatRegimenFiscal, SatUsoCfdi, SatFormaPago, SatMetodoPago, Moneda

class Cliente(StatusLifecycleModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="clientes", null=True, blank=True)
    vendedores = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="clientes_asignados", blank=True)
    razon_social = models.CharField(max_length=255, blank=True)
    nombre = models.CharField(max_length=150)
    telefono = models.CharField(max_length=20, blank=True)
    correo = models.EmailField(max_length=254, blank=True)
    rfc = models.CharField(max_length=13, blank=True)
    sat_regimen_fiscal = models.ForeignKey(SatRegimenFiscal, on_delete=models.CASCADE, related_name="clientes", null=True, blank=True)
    direccion_fiscal = models.CharField(max_length=255, blank=True)
    colonia = models.CharField(max_length=120, blank=True)
    codigo_postal = models.CharField(max_length=10, blank=True)
    ciudad = models.CharField(max_length=120, blank=True)
    estado = models.CharField(max_length=120, blank=True)
    giro_empresarial = models.CharField(max_length=150, blank=True)
    activo = models.BooleanField(default=True)

    sat_uso_cfdi = models.ForeignKey(SatUsoCfdi, on_delete=models.CASCADE, related_name="clientes", null=True, blank=True)

    class Meta:
        db_table = "clientes"
        verbose_name = "Cliente"
        verbose_name_plural = "Clientes"

    def __str__(self):
        return str(self.id)

class Proveedor(StatusLifecycleModel):
    class TipoProveedor(models.TextChoices):
        GENERAL = "General", "General"
        COMPRAS = "Compras", "Compras"
        IMPUESTOS = "Impuestos", "Impuestos"
        DATOS_BANCARIOS = "Datos Bancarios", "Datos Bancarios"
        VARIOS = "Varios", "Varios"
        PRODUCCION = "Produccion", "Produccion"

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="proveedores", null=True, blank=True)
    nombre = models.CharField(max_length=150)
    sat_regimen_fiscal = models.ForeignKey(SatRegimenFiscal, on_delete=models.CASCADE, related_name="proveedores")
    sat_forma_pago = models.ForeignKey(SatFormaPago, on_delete=models.CASCADE, related_name="proveedores")
    sat_metodo_pago = models.ForeignKey(SatMetodoPago, on_delete=models.CASCADE, related_name="proveedores")
    activo = models.BooleanField(default=True)
    fecha_alta = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    #catalogo de proveedores
    codigo = models.CharField(max_length=30)
    razon_social = models.CharField(max_length=180)
    direccion = models.CharField(max_length=255, blank=True, null=True)
    colonia = models.CharField(max_length=120, blank=True, null=True)
    ciudad = models.CharField(max_length=120, blank=True, null=True)
    codigo_postal = models.CharField(max_length=10, blank=True, null=True)
    telefono = models.CharField(max_length=30)
    estado = models.CharField(max_length=120, blank=True, null=True)
    fax = models.CharField(max_length=30) #opcional
    contacto_principal = models.CharField(max_length=120)
    observaciones = models.TextField(max_length=255, blank=True, null=True)
    curp = models.CharField(max_length=18, blank=True, null=True)
    rfc = models.CharField(max_length=20)
    email = models.EmailField(max_length=120)
    tipo = models.CharField(max_length=50, choices=TipoProveedor.choices, default=TipoProveedor.GENERAL)
    #condiciones
    lista_precios = models.PositiveSmallIntegerField(default=1)
    descuento_monto = models.DecimalField(max_digits=12,decimal_places=2, default=0)
    descuento_porcentaje = models.DecimalField(max_digits=5,decimal_places=2, default=0)
    plazo_dias = models.PositiveIntegerField(default=0, help_text="Plazo de crédito acordado con el proveedor.")
    aplicar_a = models.CharField(max_length=50, blank=True)
    limite_credito = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    cuenta_contable = models.CharField(max_length=20, blank=True)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE, related_name="proveedores")

    dias_credito = models.IntegerField(default=0)
    #acumulados
    plazo_real_dias = models.PositiveIntegerField(default=0, help_text="Promedio real de días utilizados para pagar.")
    fecha_ultima_compra = models.DateField(null=True,blank=True)
    fecha_ultimo_pago = models.DateField(null=True,blank=True)
    saldo_anterior = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    fecha_baja = models.DateField(null=True,blank=True)
    saldo_actual = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    saldo_acumulado = models.DecimalField(max_digits=15, decimal_places=2, default=0)


    class Meta:
        db_table = "proveedores"
        verbose_name = "Proveedor"
        verbose_name_plural = "Proveedores"
    
    def __str__(self):
        return str(self.id)

class DireccionCliente(StatusLifecycleModel):
    destinatario = models.CharField(max_length=150)
    empresa_envio = models.CharField(max_length=150)
    telefono_envio = models.CharField(max_length=20)
    celular_envio = models.CharField(max_length=20)
    direccion_envio = models.CharField(max_length=255)
    colonia_envio = models.CharField(max_length=120)
    codigo_postal = models.CharField(max_length=10)
    ciudad_envio = models.CharField(max_length=120)
    estado_envio = models.CharField(max_length=120)
    referencias = models.TextField(blank=True, null=True)
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name="direcciones")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="direcciones_clientes")
    is_default = models.BooleanField(default=False)
    activo = models.BooleanField(default=True, blank=True)

    class Meta:
        db_table = "direcciones_cliente"
        verbose_name = "Direccion Cliente"
        verbose_name_plural = "Direcciones Clientes"

class Transportista(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="transportistas")

    class Meta:
        db_table = "transportistas"
        verbose_name = "Transportista"
        verbose_name_plural = "Transportistas"
