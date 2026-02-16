from django.db import models
from nucleo.models import Empresa, UnidadMedida, Impuesto, SatClaveProdServ, SatClaveUnidad

class TipoProducto(models.Model):
    codigo = models.CharField(max_length=10)

    class Meta:
        db_table = "tipo_producto"
        verbose_name = "Tipo Producto"
        verbose_name_plural = "Tipos Producto"

class CategoriaProducto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="categorias_producto")

    class Meta:
        db_table = "categorias_producto"
        verbose_name = "Categoria Producto"
        verbose_name_plural = "Categorias Producto"

class Producto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="productos")
    categoria_producto = models.ForeignKey(CategoriaProducto, on_delete=models.CASCADE, related_name="productos")
    unidad_medida = models.ForeignKey(UnidadMedida, on_delete=models.CASCADE, related_name="productos")
    impuesto = models.ForeignKey(Impuesto, on_delete=models.CASCADE, related_name="productos")
    sat_prodserv = models.ForeignKey(SatClaveProdServ, on_delete=models.CASCADE, related_name="productos")
    sat_unidad = models.ForeignKey(SatClaveUnidad, on_delete=models.CASCADE, related_name="productos")

    class Meta:
        db_table = "productos"
        verbose_name = "Producto"
        verbose_name_plural = "Productos"


