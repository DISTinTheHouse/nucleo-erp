from django.db import models
from nucleo.models import Empresa, Sucursal


class Almacen(models.Model):
    id_almacen = models.BigAutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="almacenes", null=True, blank=True)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="almacenes", null=True, blank=True)
    codigo = models.CharField(max_length=50, default='')
    nombre = models.CharField(max_length=255, default='')
    estatus = models.CharField(max_length=20, default='activo')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "almacenes"
        verbose_name = "Almacen"
        verbose_name_plural = "Almacenes"
        constraints = [
            models.UniqueConstraint(fields=['sucursal', 'codigo'], name='uq_almacen_sucursal_codigo')
        ]
        indexes = [
            models.Index(fields=['empresa', 'estatus'])
        ]

    def __str__(self):
        return f"{self.sucursal.nombre} - {self.nombre}"


class Ubicacion(models.Model):
    id_ubicacion = models.BigAutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="ubicaciones", null=True, blank=True)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="ubicaciones", null=True, blank=True)
    almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE, related_name="ubicaciones", null=True, blank=True)
    codigo = models.CharField(max_length=50, default='')
    nombre = models.CharField(max_length=255, default='')
    estatus = models.CharField(max_length=20, default='activo')
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "ubicaciones"
        verbose_name = "Ubicacion"
        verbose_name_plural = "Ubicaciones"
        constraints = [
            models.UniqueConstraint(fields=['almacen', 'codigo'], name='uq_ubicacion_almacen_codigo')
        ]
        indexes = [
            models.Index(fields=['empresa', 'estatus'])
        ]

    def __str__(self):
        return f"{self.almacen.nombre} - {self.nombre}"

