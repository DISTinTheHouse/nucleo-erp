from django.db import models
from nucleo.models import Empresa, UnidadMedida, Impuesto, SatClaveProdServ, SatClaveUnidad

class TipoProducto(models.Model):
    codigo = models.CharField(max_length=50)

    class Meta:
        db_table = "tipo_producto"
        verbose_name = "Tipo Producto"
        verbose_name_plural = "Tipos Producto"

class CategoriaProducto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="categorias_producto")
    nombre = models.CharField(max_length=100)
    codigo = models.CharField(max_length=3)
    descripcion = models.CharField(max_length=150)
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "categorias_producto"
        verbose_name = "Categoria Producto"
        verbose_name_plural = "Categorias Producto"
    
    def __str__(self):
        return self.nombre

class Color(models.Model):
    nombre = models.CharField(max_length=50)
    codigo = models.CharField(max_length=3)
    codigo_hex = models.CharField(max_length=7)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "colores"
        verbose_name = "Color"
        verbose_name_plural = "Colores"
    
    def __str__(self):
        return self.nombre

class Talla(models.Model):
    nombre = models.CharField(max_length=50)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "tallas"
        verbose_name = "Talla"
        verbose_name_plural = "Tallas"
    
    def __str__(self):
        return self.nombre

class Producto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="productos")
    categoria_producto = models.ForeignKey(CategoriaProducto, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    unidad_medida = models.ForeignKey(UnidadMedida, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    impuesto = models.ForeignKey(Impuesto, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    sat_prodserv = models.ForeignKey(SatClaveProdServ, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    sat_unidad = models.ForeignKey(SatClaveUnidad, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    nombre = models.CharField(max_length=100)
    descripcion = models.CharField(max_length=150, blank=True, default="")
    tipo = models.CharField(max_length=35, blank=True, default="")
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # campos para costo
    # costo_base = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    precio_base = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "productos"
        verbose_name = "Producto"
        verbose_name_plural = "Productos"
    
    def __str__(self):
        return self.nombre

class ProductoVariante(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="variantes")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="variantes")
    color = models.ForeignKey(Color, on_delete=models.CASCADE, related_name="variantes")
    talla = models.ForeignKey(Talla, on_delete=models.CASCADE, related_name="variantes")
    sku = models.CharField(max_length=50, unique=True)
    precio_base = models.DecimalField(max_digits=10, decimal_places=2)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "variantes_producto"
        verbose_name = "Variante Producto"
        verbose_name_plural = "Variantes Producto"

    def __str__(self):
        return f"{self.producto.nombre} - {self.color.nombre} - {self.talla.nombre}"

class VarianteProductoProduccion(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="variantes_produccion")
    op = models.ForeignKey('produccion.OrdenProduccion', on_delete=models.CASCADE, related_name="variantes_produccion")
    producto_base = models.ForeignKey(Producto, on_delete=models.CASCADE, null=True, blank=True, related_name="variantes_produccion")
    color = models.ForeignKey(Color, on_delete=models.CASCADE, null=True, blank=True, related_name="variantes_produccion")
    talla = models.ForeignKey(Talla, on_delete=models.CASCADE, null=True, blank=True, related_name="variantes_produccion")

    class Meta:
        db_table = "variantes_producto_produccion"
        verbose_name = "Variante Producto Produccion"
        verbose_name_plural = "Variantes Producto Produccion"
    

