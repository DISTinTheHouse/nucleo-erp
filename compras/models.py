from django.db import models
from nucleo.models import Empresa, Sucursal, Departamento, Moneda
from terceros.models import Proveedor
from catalogo.models import Producto
from inventarios.models import Almacen, Ubicacion, Lote, Serie

class Requisicion(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE) 
    departamento = models.ForeignKey(Departamento, on_delete=models.CASCADE)

    class Meta:
        db_table = 'requisiciones'
        verbose_name = 'Requisición'
        verbose_name_plural = 'Requisiciones'

    def __str__(self):
        return str(self.id)

class RequisicionDetalle(models.Model):
    requisicion = models.ForeignKey(Requisicion, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)

    class Meta:
        db_table = 'requisicion_detalle'
        verbose_name = 'Requisición Detalle'
        verbose_name_plural = 'Requisiciones Detalle'

    def __str__(self):
        return str(self.id)

class CotizacionProveedor(models.Model):
    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE)
    requisicion = models.ForeignKey(Requisicion, on_delete=models.CASCADE)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE)

    class Meta:
        db_table = 'cotizaciones_proveedor'
        verbose_name = 'Cotización Proveedor'
        verbose_name_plural = 'Cotizaciones Proveedor'

    def __str__(self):
        return str(self.id)

class CotizacionProveedorDetalle(models.Model):
    cotizacion_proveedor = models.ForeignKey(CotizacionProveedor, on_delete=models.CASCADE)
    requisicion_detalle = models.ForeignKey(RequisicionDetalle, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)

    class Meta:
        db_table = 'cotizacion_proveedor_detalle'
        verbose_name = 'Cotización Proveedor Detalle'
        verbose_name_plural = 'Cotizaciones Proveedor Detalle'

    def __str__(self):
        return str(self.id)

class SolicitudCompra(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE) 
    departamento = models.ForeignKey(Departamento, on_delete=models.CASCADE)
    requisicion = models.ForeignKey(Requisicion, on_delete=models.CASCADE)

    class Meta:
        db_table = 'solicitudes_compra'
        verbose_name = 'Solicitud Compra'
        verbose_name_plural = 'Solicitudes Compra'

    def __str__(self):
        return str(self.id)

class SolicitudCompraDetalle(models.Model):
    solicitud_compra = models.ForeignKey(SolicitudCompra, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    requisicion_detalle = models.ForeignKey(RequisicionDetalle, on_delete=models.CASCADE)

    class Meta:
        db_table = 'solicitud_compra_detalle'
        verbose_name = 'Solicitud Compra Detalle'
        verbose_name_plural = 'Solicitudes Compra Detalle'

    def __str__(self):
        return str(self.id)

class OrdenCompra(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE) 
    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE)
    solicitud_compra = models.ForeignKey(SolicitudCompra, on_delete=models.CASCADE)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE)

    class Meta:
        db_table = 'ordenes_compra'
        verbose_name = 'Orden Compra'
        verbose_name_plural = 'Ordenes Compra'

    def __str__(self):
        return str(self.id)

class OrdenCompraDetalle(models.Model):
    orden_compra = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    solicitud_compra_detalle = models.ForeignKey(SolicitudCompraDetalle, on_delete=models.CASCADE)
    requisicion_detalle = models.ForeignKey(RequisicionDetalle, on_delete=models.CASCADE)

    class Meta:
        db_table = 'orden_compra_detalle'
        verbose_name = 'Orden Compra Detalle'
        verbose_name_plural = 'Ordenes Compra Detalle'

    def __str__(self):
        return str(self.id)

class Recepcion(models.Model):
    orden_compra = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE)
    almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE)

    class Meta:
        db_table = 'recepciones'
        verbose_name = 'Recepcion'
        verbose_name_plural = 'Recepciones'

    def __str__(self):
        return str(self.id)

class RecepcionDetalle(models.Model):
    recepcion = models.ForeignKey(Recepcion, on_delete=models.CASCADE)
    orden_compra_detalle = models.ForeignKey(OrdenCompraDetalle, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.CASCADE)
    lote = models.ForeignKey(Lote, on_delete=models.CASCADE)
    serie = models.ForeignKey(Serie, on_delete=models.CASCADE)

    class Meta:
        db_table = 'recepcion_detalle'
        verbose_name = 'Recepcion Detalle'
        verbose_name_plural = 'Recepciones Detalle'

    def __str__(self):
        return str(self.id)