from django.db import models
from nucleo.models import StatusLifecycleModel
from simple_history.models import HistoricalRecords

class CuentaContable(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="cuentas_contables")

    class Meta:
        db_table = "cuentas_contables"
        verbose_name = "Cuenta Contable"
        verbose_name_plural = "Cuentas Contables"

    def __str__(self):
        return str(self.id)

class CentroCosto(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="centro_costos")

    class Meta:
        db_table = "centros_costo"
        verbose_name = "Centro de Costo"
        verbose_name_plural = "Centros de Costo"
    
    def __str__(self):
        return str(self.id)

class Poliza(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="polizas")
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.CASCADE, related_name="polizas")

    class Meta:
        db_table = "polizas"
        verbose_name = "Poliza"
        verbose_name_plural = "Polizas"
    
    def __str__(self):
        return str(self.id)

class Factura(StatusLifecycleModel):
    class FacturaStatus(models.TextChoices):
        BORRADOR = 'Borrador', 'Borrador'
        EMITIDA = 'Emitida', 'Emitida'
        CANCELADA = 'Cancelada', 'Cancelada'
        
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="facturas")
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.CASCADE, related_name="facturas")
    cliente = models.ForeignKey('terceros.Cliente', on_delete=models.CASCADE, related_name="facturas")
    pedido = models.ForeignKey('ventas.Pedido', on_delete=models.CASCADE, related_name="facturas")
    serie_folio = models.ForeignKey('nucleo.SerieFolio', on_delete=models.CASCADE, related_name="facturas", null=True, blank=True)
    moneda = models.ForeignKey('nucleo.Moneda', on_delete=models.CASCADE, related_name="facturas")

    fecha_emision = models.DateField(auto_now=True)
    fecha_vencimiento = models.DateField(null=True, blank=True)
    folio = models.CharField(max_length=30, null=True, blank=True)

    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    descuento = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    impuestos = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    estatus = models.CharField(max_length=30, choices=FacturaStatus.choices, default=FacturaStatus.BORRADOR)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        db_table = "facturas"
        verbose_name = "Factura"
        verbose_name_plural = "Facturas"
    
    def __str__(self):
        return str(self.id)

class FacturaDetalle(models.Model):
    factura = models.ForeignKey(Factura, on_delete=models.CASCADE, related_name="factura_detalles")
    pedido_detalle = models.ForeignKey('ventas.PedidoDetalle', on_delete=models.CASCADE, related_name="factura_detalles")
    producto = models.ForeignKey('catalogo.Producto', on_delete=models.CASCADE, related_name="factura_detalles")

    cantidad = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    precio_unitario = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    descuento = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    impuesto = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    
    class Meta:
        db_table = "factura_detalle"
        verbose_name = "Factura Detalle"
        verbose_name_plural = "Facturas Detalle"
    
    def __str__(self):
        return str(self.id)

class FacturaProveedor(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="facturas_proveedores")
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.CASCADE, related_name="facturas_proveedores")
    proveedor = models.ForeignKey('terceros.Proveedor', on_delete=models.CASCADE, related_name="facturas_proveedores")
    oc = models.ForeignKey('compras.OrdenCompra', on_delete=models.CASCADE, related_name="facturas_proveedores")
    recepcion = models.ForeignKey('compras.Recepcion', on_delete=models.CASCADE, related_name="facturas_proveedores")
    moneda = models.ForeignKey('nucleo.Moneda', on_delete=models.CASCADE, related_name="facturas_proveedores")

    class Meta:
        db_table = "facturas_proveedor"
        verbose_name = "Factura Proveedor"
        verbose_name_plural = "Facturas Proveedor"
    
    def __str__(self):
        return str(self.id)

class FacturaProveedorDetalle(models.Model):
    factura_proveedor = models.ForeignKey(FacturaProveedor, on_delete=models.CASCADE, related_name="factura_proveedor_detalles")
    oc_detalle = models.ForeignKey('compras.OrdenCompraDetalle', on_delete=models.CASCADE, related_name="factura_proveedor_detalles")
    recepcion_detalle = models.ForeignKey('compras.RecepcionDetalle', on_delete=models.CASCADE, related_name="factura_proveedor_detalles")
    producto = models.ForeignKey('catalogo.Producto', on_delete=models.CASCADE, related_name="factura_proveedor_detalles")

    class Meta:
        db_table = "factura_proveedor_detalle"
        verbose_name = "Factura Proveedor Detalle"
        verbose_name_plural = "Facturas Proveedor Detalle"

    def __str__(self):
        return str(self.id)

class Banco(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="bancos")

    class Meta:
        db_table = "bancos"
        verbose_name = "Banco"
        verbose_name_plural = "Bancos"
    
    def __str__(self):
        return str(self.id)

class CuentaBancaria(models.Model):
    banco = models.ForeignKey(Banco, on_delete=models.CASCADE, related_name="cuentas_bancarias")
    moneda = models.ForeignKey('nucleo.Moneda', on_delete=models.CASCADE, related_name="cuentas_bancarias")

    class Meta:
        db_table = "cuentas_bancarias"
        verbose_name = "Cuenta Bancaria"
        verbose_name_plural = "Cuentas Bancarias"

    def __str__(self):
        return str(self.id)

class CuentaPorCobrar(models.Model):
    class EstatusCxC(models.TextChoices):
        PENDIENTE = 'Pendiente', 'Pendiente'
        PARCIAL = 'Parcial', 'Parcial'
        PAGADA = 'Pagada', 'Pagada'
        CANCELADA = 'Cancelada', 'Cancelada'
        VENCIDA = 'Vencida', 'Vencida'

    cliente = models.ForeignKey('terceros.Cliente', on_delete=models.CASCADE, related_name="cuentas_por_cobrar")
    factura = models.ForeignKey(Factura, on_delete=models.CASCADE, related_name="cuentas_por_cobrar")
    fecha_emision = models.DateField(auto_now=True)
    fecha_vencimiento = models.DateField(null=True, blank=True)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    saldo = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    estatus = models.CharField(max_length=30, choices=EstatusCxC.choices, default=EstatusCxC.PENDIENTE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "cuentas_por_cobrar"
        verbose_name = "Cuenta Por Cobrar"
        verbose_name_plural = "Cuentas Por Cobrar"

    def __str__(self):
        return str(self.id)

class CuentaPorPagar(models.Model):
    proveedor = models.ForeignKey('terceros.Proveedor', on_delete=models.CASCADE, related_name="cuentas_por_pagar")
    factura_proveedor = models.ForeignKey(FacturaProveedor, on_delete=models.CASCADE, related_name="cuentas_por_pagar")

    class Meta:
        db_table = "cuentas_por_pagar"
        verbose_name = "Cuenta Por Pagar"
        verbose_name_plural = "Cuentas Por Pagar"

    def __str__(self):
        return str(self.id)

class Cobro(models.Model):
    class MetodoPago(models.TextChoices):
        EFECTIVO = 'Efectivo', 'Efectivo'
        TRANSFERENCIA = 'Transferencia', 'Transferencia'
        TARJETA = 'Tarjeta', 'Tarjeta'
        CHEQUE = 'Cheque', 'Cheque'
    
    class OpcionesReferencia(models.TextChoices):
        SPEI = 'SPEI', 'SPEI'
        CHEQUE = 'Cheque', 'Cheque'
        AUTORIZACION = 'Autorizacion TPV', 'Autorizacion TPV'

    cliente = models.ForeignKey('terceros.Cliente', on_delete=models.CASCADE, related_name="cobros")
    cuenta_bancaria = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE, related_name="cobros")

    fecha_cobro = models.DateField(auto_now=True)
    metodo_pago = models.CharField(max_length=30, choices=MetodoPago.choices, default=MetodoPago.EFECTIVO)
    referencia = models.CharField(max_length=30, choices=OpcionesReferencia.choices, default=OpcionesReferencia.SPEI)

    total_cobrado = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "cobros"
        verbose_name = "Cobro"
        verbose_name_plural = "Cobros"

    def __str__(self):
        return str(self.id)

class CobroDetalle(models.Model):
    cobro = models.ForeignKey(Cobro, on_delete=models.CASCADE, related_name="cobro_detalles")
    cxc = models.ForeignKey(CuentaPorCobrar, on_delete=models.CASCADE, related_name="cobro_detalles")

    importe_aplicado = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "cobro_detalle"
        verbose_name = "Cobro Detalle"
        verbose_name_plural = "Cobros Detalle"

    def __str__(self):
        return str(self.id)

class Pago(models.Model):
    proveedor = models.ForeignKey('terceros.Proveedor', on_delete=models.CASCADE, related_name="pagos")
    cuenta_bancaria = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE, related_name="pagos")

    class Meta:
        db_table = "pagos"
        verbose_name = "Pago"
        verbose_name_plural = "Pagos"
    
    def __str__(self):
        return str(self.id)

class PagoDetalle(models.Model):
    pago = models.ForeignKey(Pago, on_delete=models.CASCADE, related_name="pago_detalles")
    cxp = models.ForeignKey(CuentaPorPagar, on_delete=models.CASCADE, related_name="pago_detalles")

    class Meta:
        db_table = "pago_detalle"
        verbose_name = "Pago Detalle"
        verbose_name_plural = "Pagos Detalle"

    
    def __str__(self):
        return str(self.id)

class MovimientoBancario(models.Model):
    cuenta_bancaria = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE, related_name="movimientos_bancarios")
    pago = models.ForeignKey(Pago, on_delete=models.CASCADE, related_name="movimientos_bancarios", null=True, blank=True)
    cobro = models.ForeignKey(Cobro, on_delete=models.CASCADE, related_name="movimientos_bancarios", null=True, blank=True)

    class Meta:
        db_table = "movimientos_bancarios"
        verbose_name = "Movimiento Bancario"
        verbose_name_plural = "Movimientos Bancarios"

    def __str__(self):
        return str(self.id)

class PolizaDetalle(models.Model):
    poliza = models.ForeignKey(Poliza, on_delete=models.CASCADE, related_name="poliza_detalles")
    cuenta_contable = models.ForeignKey(CuentaContable, on_delete=models.CASCADE, related_name="poliza_detalles")
    centro_costo = models.ForeignKey(CentroCosto, on_delete=models.CASCADE, related_name="poliza_detalles")
    
    factura = models.ForeignKey(Factura, on_delete=models.CASCADE, related_name="poliza_detalles", null=True, blank=True)
    factura_proveedor = models.ForeignKey(FacturaProveedor, on_delete=models.CASCADE, related_name="poliza_detalles", null=True, blank=True)
    pago = models.ForeignKey(Pago, on_delete=models.CASCADE, related_name="poliza_detalles", null=True, blank=True)
    cobro = models.ForeignKey(Cobro, on_delete=models.CASCADE, related_name="poliza_detalles", null=True, blank=True)
    movimiento_bancario = models.ForeignKey(MovimientoBancario, on_delete=models.CASCADE, related_name="poliza_detalles", null=True, blank=True)

    class Meta:
        db_table = "poliza_detalle"
        verbose_name = "Poliza Detalle"
        verbose_name_plural = "Polizas Detalle"

    def __str__(self):
        return str(self.id)

class ConciliacionBancaria(models.Model):
    cuenta_bancaria = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE, related_name="conciliaciones_bancarias")

    class Meta:
        db_table = "conciliaciones_bancarias"
        verbose_name = "Conciliacion Bancaria"
        verbose_name_plural = "Conciliaciones Bancarias"
    
    def __str__(self):
        return str(self.id)

class ConciliacionDetalle(models.Model):
    conciliacion = models.ForeignKey(ConciliacionBancaria, on_delete=models.CASCADE, related_name="conciliacion_detalles")
    movimiento_bancario = models.ForeignKey(MovimientoBancario, on_delete=models.CASCADE, related_name="conciliacion_detalles")

    class Meta:
        db_table = "conciliacion_detalle"
        verbose_name = "Conciliacion Detalle"
        verbose_name_plural = "Conciliaciones Detalle"

    def __str__(self):
        return str(self.id)

class NotaCredito(models.Model):
    factura = models.ForeignKey(Factura, on_delete=models.CASCADE, related_name="nota_creditos")
    cliente = models.ForeignKey('terceros.Cliente', on_delete=models.CASCADE, related_name="nota_creditos")

    class Meta:
        db_table = "notas_creditos"
        verbose_name = "Nota Credito"
        verbose_name_plural = "Notas Creditos"
    
    def __str__(self):
        return str(self.id)

class NotaCreditoDetalle(models.Model):
    nota_credito = models.ForeignKey(NotaCredito, on_delete=models.CASCADE, related_name="nota_credito_detalles")
    factura_detalle = models.ForeignKey(FacturaDetalle, on_delete=models.CASCADE, related_name="nota_credito_detalles")


    class Meta:
        db_table = "nota_credito_detalle"
        verbose_name = "Nota Credito Detalle"
        verbose_name_plural = "Notas Creditos Detalle"
    
    def __str__(self):
        return str(self.id)