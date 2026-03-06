from django.db import models
from nucleo.models import Empresa, SatRegimenFiscal, SatUsoCfdi

class Cliente(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="clientes", null=True, blank=True)
    sat_regimen_fiscal = models.ForeignKey(SatRegimenFiscal, on_delete=models.CASCADE, related_name="clientes", null=True, blank=True)
    sat_uso_cfdi = models.ForeignKey(SatUsoCfdi, on_delete=models.CASCADE, related_name="clientes", null=True, blank=True)

    class Meta:
        db_table = "clientes"
        verbose_name = "Cliente"
        verbose_name_plural = "Clientes"

    def __str__(self):
        return str(self.id)

class Proveedor(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="proveedores", null=True, blank=True)

    class Meta:
        db_table = "proveedores"
        verbose_name = "Proveedor"
        verbose_name_plural = "Proveedores"
    
    def __str__(self):
        return str(self.id)

class DireccionCliente(models.Model):
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name="direcciones")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="direcciones_clientes")
    is_default = models.BooleanField(default=False)
    activo = models.BooleanField(default=True, blank=True)

    class Meta:
        db_table = "direcciones_cliente"
        verbose_name = "Direccion Cliente"
        verbose_name_plural = "Direcciones Clientes"