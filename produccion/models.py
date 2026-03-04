from django.db import models
from nucleo.models import Empresa, Sucursal
from catalogo.models import Producto
from ventas.models import Pedido
from inventarios.models import Almacen, Ubicacion

class ListaMaterialBom(models.Model):
    bom_id = models.AutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)

    class Meta:
        db_table = 'listas_materiales_bom'
        verbose_name = 'Lista Material bom'
        verbose_name_plural = 'Listas Materiales bom'
    
    def __str__(self):
        return str(self.bom_id)

class RutaProduccion(models.Model):
    ruta_produccion_id = models.AutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)

    class Meta:
        db_table = 'rutas_produccion'
        verbose_name = 'Ruta Produccion'
        verbose_name_plural = 'Rutas Produccion'
    
    def __str__(self):
        return str(self.ruta_produccion_id)
    
class OrdenProduccion(models.Model):
    op_id = models.AutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE)
    ruta_produccion = models.ForeignKey(RutaProduccion, on_delete=models.CASCADE)

    class Meta:
        db_table = 'ordenes_produccion'
        verbose_name = 'Orden Produccion'
        verbose_name_plural = 'Ordenes Produccion'
    
    def __str__(self):
        return str(self.op_id)

class ConsumoProduccion(models.Model):
    consumo_produccion_id = models.AutoField(primary_key=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE)

    class Meta:
        db_table = 'consumos_produccion'
        verbose_name = 'Consumo Produccion'
        verbose_name_plural = 'Consumos Produccion'

    def __str__(self):
        return str(self.consumo_produccion_id)

class ProductoTerminadoEntradas(models.Model):
    pt_entrada_id = models.AutoField(primary_key=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE)
    almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE)
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.CASCADE)

    class Meta:
        db_table = 'producto_terminado_entradas'
        verbose_name = 'Producto Terminado Entrada'
        verbose_name_plural = 'Producto Terminado Entradas'

    def __str__(self):
        return str(self.pt_entrada_id)