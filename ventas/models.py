from django.db import models
from nucleo.models import Empresa, Sucursal, Moneda
from terceros.models import Cliente
from catalogo.models import Producto

class Prospecto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="prospectos")
    
    class Meta:
        db_table = "prospectos"
        verbose_name = "Prospecto"
        verbose_name_plural = "Prospectos"
    
    def __str__(self):
        return str(self.id)

class oportunidad(models.Model):
    prospecto = models.ForeignKey(Prospecto, on_delete=models.CASCADE, related_name="oportunidades")

    class Meta:
        db_table = "oportunidades"
        verbose_name = "Oportunidad"
        verbose_name_plural = "Oportunidades"
    
    def __str__(self):
        return str(self.id)

class Cotizacion(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="cotizacion")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="cotizacion")
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name="cotizacion")
    oportunidad = models.ForeignKey(oportunidad, on_delete=models.CASCADE, related_name="cotizacion", null=True)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE, related_name="cotizacion")

    class Meta:
        db_table = "cotizaciones"
        verbose_name = "Cotización"
        verbose_name_plural = "Cotizaciones"
    
    def __str__(self):
        return str(self.id)

class CotizacionDetalle(models.Model):
    cotizacion = models.ForeignKey(Cotizacion, on_delete=models.CASCADE, related_name="cotizaciondetalle")
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="cotizaciondetalle")

    class Meta:
        db_table = "cotizacion_detalle"
        verbose_name = "Cotización Detalle"
        verbose_name_plural = "Cotizaciones Detalle"
    
    def __str__(self):
        return str(self.id)

class Pedido(models.Model):
    CHOICES_ESTATUS = (
        (1, "Borrador"),
        (2, "Por Autorizar"),
        (3, "Autorizado"),
        (4, "En Proceso"),
        (5, "Cerrado"),
    )
    CHOICES_TIPO_PEDIDO = (
        (1, "Stock"),
        (2, "Fabricacion"),
        (3, "Muestra"),
        (4, "Mixto"),
    )
        
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="pedidos")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="pedidos")
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name="pedidos")
    cotizacion = models.ForeignKey(Cotizacion, on_delete=models.CASCADE, related_name="pedidos", null=True)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE, related_name="pedidos")
    tipo_pedido = models.SmallIntegerField(default=1, choices=CHOICES_TIPO_PEDIDO)
    estatus = models.SmallIntegerField(default=1, choices=CHOICES_ESTATUS)

    class Meta:
        db_table = "pedidos"
        verbose_name = "Pedido"
        verbose_name_plural = "Pedidos"
    
    def __str__(self):
        return str(self.id)
    
class PedidoDetalle(models.Model):
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="detalles")
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="detalles")

    class Meta:
        db_table = "pedido_detalle"
        verbose_name = "Pedido Detalle"
        verbose_name_plural = "Pedidos Detalle"
    
    def __str__(self):
        return str(self.id)

class Entrega(models.Model):
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="entregas")

    class Meta:
        db_table = "entregas"
        verbose_name = "Entrega"
        verbose_name_plural = "Entregas"

    def __str__(self):
        return str(self.id)

class EntregaDetalle(models.Model):
    entrega = models.ForeignKey(Entrega, on_delete=models.CASCADE, related_name="detalles")
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE, related_name="entregas")

    class Meta:
        db_table = "entrega_detalle"
        verbose_name = "Entrega Detalle"
        verbose_name_plural = "Entregas Detalles"

    def __str__(self):
        return str(self.id)

class Devolucion(models.Model):
    entrega = models.ForeignKey(Entrega, on_delete=models.CASCADE, related_name="devoluciones")
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="devoluciones")

    class Meta:
        db_table = "devoluciones"
        verbose_name = "Devolución"
        verbose_name_plural = "Devoluciones"
    
    def __str__(self):
        return str(self.id)

class DevolucionDetalle(models.Model):
    devolucion = models.ForeignKey(Devolucion, on_delete=models.CASCADE, related_name="detalles")
    entrega_detalle = models.ForeignKey(EntregaDetalle, on_delete=models.CASCADE, related_name="devoluciones")
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE, related_name="devoluciones")

    class Meta:
        db_table = "devolucion_detalle"
        verbose_name = "Devolución Detalle"
        verbose_name_plural = "Devoluciones Detalle"
    
    def __str__(self):
        return str(self.id)
