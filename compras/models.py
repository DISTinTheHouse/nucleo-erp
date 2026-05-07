from django.db import models
from django.conf import settings
from nucleo.models import Empresa, Sucursal, Departamento, Moneda, StatusLifecycleModel
from terceros.models import Proveedor, Transportista
from catalogo.models import Producto
from ventas.models import Pedido

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

class OrdenCompra(StatusLifecycleModel):
    class EstatusOrdenCompra(models.IntegerChoices):
        BORRADOR = 1, 'Borrador'
        POR_AUTORIZAR = 2, 'Por autorizar'
        AUTORIZADA = 3, 'Autorizada'
        PARCIALMENTE_RECIBIDA = 4, 'Parcialmente recibida'
        RECIBIDA = 5, 'Recibida'
        CANCELADA = 6, 'Cancelada'

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE)
    solicitud_compra = models.ForeignKey(SolicitudCompra, on_delete=models.CASCADE)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name='ordenes_compra', null=True)

    folio = models.CharField(max_length=30, unique=True)
    referencia = models.CharField(max_length=50, null=True)

    fecha_oc = models.DateField()
    fecha_entrega_estimada = models.DateField(null=True)
    fecha_autorizacion = models.DateTimeField(null=True)
    fecha_vencimiento = models.DateField(null=True)
    
    estatus = models.IntegerField(choices=EstatusOrdenCompra.choices, default=EstatusOrdenCompra.BORRADOR)

    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    descuento = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    impuestos = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)

    tipo = models.CharField(max_length=20, default='ODC')
    #totales y financieros
    total_piezas = models.IntegerField(default=0)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    descuento = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    flete = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    seguros = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    porcentaje_iva = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    total_iva = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    gran_total = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    a_cuenta = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)

    observaciones = models.TextField(blank=True, null=True)

    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
    descripcion = models.CharField(max_length=200, null=True)
    cantidad = models.IntegerField(default=0)
    precio = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    descuento = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    importe = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE, related_name='ordenes_compra_detalle')
    piezas = models.IntegerField(default=0)

    class Meta:
        db_table = 'orden_compra_detalle'
        verbose_name = 'Orden Compra Detalle'
        verbose_name_plural = 'Ordenes Compra Detalle'

    def __str__(self):
        return str(self.id)

class Recepcion(models.Model):
    class EstatusRecepcion(models.IntegerChoices):
        BORRADOR = 1, 'Borrador'
        RECIBIDA = 2, 'Recibida'
        PARCIAL = 3, 'Parcial'
        EN_CALIDAD = 4, 'En calidad'
        CERRADA = 5, 'Cerrada'
        CANCELADA = 6, 'Cancelada'
    
    orden_compra = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name='recepciones')
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE, related_name='recepciones')
    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE, related_name='recepciones')
    almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE)
    transportista = models.ForeignKey(Transportista, on_delete=models.CASCADE, related_name='recepciones', null=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recepciones')

    folio = models.CharField(max_length=30, unique=True)
    remision = models.CharField(max_length=50, null=True)
    factura_referencia = models.CharField(max_length=50, null=True)

    fecha_recepcion = models.DateTimeField()

    estatus = models.IntegerField(choices=EstatusRecepcion.choices, default=EstatusRecepcion.BORRADOR)
    activo = models.BooleanField(default=True)

    observaciones = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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