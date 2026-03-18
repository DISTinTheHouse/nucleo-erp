from django.db import models
from nucleo.models import StatusLifecycleModel
from nucleo.models import Empresa, SatRegimenFiscal, SatUsoCfdi, SatFormaPago, SatMetodoPago, Moneda

class Cliente(StatusLifecycleModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="clientes", null=True, blank=True)
    razon_social = models.CharField(max_length=255)
    nombre = models.CharField(max_length=150)
    telefono = models.CharField(max_length=20)
    correo = models.EmailField(max_length=254)
    rfc = models.CharField(max_length=13)
    sat_regimen_fiscal = models.ForeignKey(SatRegimenFiscal, on_delete=models.CASCADE, related_name="clientes")
    direccion_fiscal = models.CharField(max_length=255)
    colonia = models.CharField(max_length=120)
    codigo_postal = models.CharField(max_length=10)
    ciudad = models.CharField(max_length=120)
    estado = models.CharField(max_length=120)
    giro_empresarial = models.CharField(max_length=150)
    activo = models.BooleanField(default=True)

    sat_uso_cfdi = models.ForeignKey(SatUsoCfdi, on_delete=models.CASCADE, related_name="clientes", null=True, blank=True)

    class Meta:
        db_table = "clientes"
        verbose_name = "Cliente"
        verbose_name_plural = "Clientes"

    def __str__(self):
        return str(self.id)

class Proveedor(StatusLifecycleModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="proveedores", null=True, blank=True)
    codigo = models.CharField(max_length=30)
    nombre = models.CharField(max_length=150)
    razon_social = models.CharField(max_length=180)
    rfc = models.CharField(max_length=20)
    email = models.EmailField(max_length=120)
    telefono = models.CharField(max_length=30)
    contacto_principal = models.CharField(max_length=120)

    sat_regimen_fiscal = models.ForeignKey(SatRegimenFiscal, on_delete=models.CASCADE, related_name="proveedores")
    sat_forma_pago = models.ForeignKey(SatFormaPago, on_delete=models.CASCADE, related_name="proveedores")
    sat_metodo_pago = models.ForeignKey(SatMetodoPago, on_delete=models.CASCADE, related_name="proveedores")
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE, related_name="proveedores")

    dias_credito = models.IntegerField(default=0)
    limite_credito = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)

    activo = models.BooleanField(default=True)
    fecha_alta = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

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