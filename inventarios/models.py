from django.db import models

from django.db import models
from nucleo.models import Sucursal

# Create your models here.
class Almacen(models.Model):
    id_almacen = models.BigAutoField(primary_key=True)
    id_sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE, related_name="almacenes")

    class Meta:
        db_table = "almacenes"
        verbose_name = "Almacen"
        verbose_name_plural = "Almacenes"

class Ubicacion(models.Model):
    id_ubicacion = models.BigAutoField(primary_key=True)
    id_almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE, related_name="ubicaciones")

    class Meta:
        db_table = "ubicaciones"
        verbose_name = "Ubicacion"
        verbose_name_plural = "Ubicaciones"

