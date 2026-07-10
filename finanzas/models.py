from django.conf import settings
from django.db import models
from django.utils import timezone
from nucleo.models import StatusLifecycleModel
from simple_history.models import HistoricalRecords

class CuentaContable(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="cuentas_contables")
    codigo = models.CharField(max_length=30, default="", blank=True)
    nombre = models.CharField(max_length=200, default="", blank=True)

    class CuentaTipo(models.TextChoices):
        ACTIVO = 'Activo', 'Activo'
        PASIVO = 'Pasivo', 'Pasivo'
        CAPITAL = 'Capital', 'Capital'
        INGRESO = 'Ingreso', 'Ingreso'
        GASTO = 'Gasto', 'Gasto'
        COSTO = 'Costo', 'Costo'
    
    tipo = models.CharField(max_length=30, choices=CuentaTipo.choices, default=CuentaTipo.ACTIVO.value)
    nivel = models.IntegerField(default=1)
    cuenta_padre = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    acepta_movimientos = models.BooleanField(default=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "cuentas_contables"
        verbose_name = "Cuenta Contable"
        verbose_name_plural = "Cuentas Contables"

    def __str__(self):
        return str(self.id)

class CentroCosto(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="centro_costos")
    codigo = models.CharField(max_length=30, default="", blank=True)
    nombre = models.CharField(max_length=200, default="", blank=True)
    descripcion = models.TextField(null=True, blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "centros_costo"
        verbose_name = "Centro de Costo"
        verbose_name_plural = "Centros de Costo"
    
    def __str__(self):
        return str(self.id)

class Poliza(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="polizas")
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.CASCADE, related_name="polizas")
    centro_costo = models.ForeignKey('finanzas.CentroCosto', on_delete=models.CASCADE, related_name="polizas", null=True, blank=True)
    folio = models.CharField(max_length=30, null=True, blank=True)
    folio_consecutivo = models.PositiveIntegerField(null=True, blank=True)

    class PolizaTipo(models.TextChoices):
        ACTIVO = 'Activo', 'Activo'
        PASIVO = 'Pasivo', 'Pasivo'
        DIARIO = 'Diario', 'Diario'
        AJUSTE = 'Ajuste', 'Ajuste'
        CAPITAL = 'Capital', 'Capital'
        INGRESO = 'Ingreso', 'Ingreso'
        GASTO = 'Gasto', 'Gasto'
        COSTO = 'Costo', 'Costo'
    
    tipo = models.CharField(max_length=30, choices=PolizaTipo.choices, default=PolizaTipo.ACTIVO.value)
    fecha = models.DateField(auto_now=True, null=True, blank=True)
    concepto = models.CharField(max_length=200, null=True, blank=True)

    class PolizaStatus(models.TextChoices):
        ACTIVO = 'Activo', 'Activo'
        PASIVO = 'Pasivo', 'Pasivo'
        CAPITAL = 'Capital', 'Capital'
        INGRESO = 'Ingreso', 'Ingreso'
        GASTO = 'Gasto', 'Gasto'
        COSTO = 'Costo', 'Costo'

    estatus = models.CharField(max_length=30, choices=PolizaStatus.choices, default=PolizaStatus.ACTIVO.value)
    usuario_creacion = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="polizas", null=True, blank=True)
    activo = models.BooleanField(default=True)


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
    class FacturaProveedorStatus(models.TextChoices):
        BORRADOR = 'Borrador', 'Borrador'
        REGISTRADA = 'Registrada', 'Registrada'
        CANCELADA = 'Cancelada', 'Cancelada'

    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="facturas_proveedores")
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.CASCADE, related_name="facturas_proveedores")
    proveedor = models.ForeignKey('terceros.Proveedor', on_delete=models.CASCADE, related_name="facturas_proveedores")
    oc = models.ForeignKey('compras.OrdenCompra', on_delete=models.CASCADE, related_name="facturas_proveedores")
    recepcion = models.ForeignKey('compras.Recepcion', on_delete=models.CASCADE, related_name="facturas_proveedores")
    moneda = models.ForeignKey('nucleo.Moneda', on_delete=models.CASCADE, related_name="facturas_proveedores")
    fecha_emision = models.DateField(auto_now=True)
    fecha_vencimiento = models.DateField(null=True, blank=True)
    folio = models.CharField(max_length=30, null=True, blank=True)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    descuento = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    impuestos = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    estatus = models.CharField(
        max_length=30,
        choices=FacturaProveedorStatus.choices,
        default=FacturaProveedorStatus.BORRADOR,
    )
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    activo = models.BooleanField(default=True)

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
    cantidad = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    precio_unitario = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    descuento = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    impuesto = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        db_table = "factura_proveedor_detalle"
        verbose_name = "Factura Proveedor Detalle"
        verbose_name_plural = "Facturas Proveedor Detalle"

    def __str__(self):
        return str(self.id)

class Banco(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.CASCADE, related_name="bancos")
    nombre = models.CharField(max_length=150, null=True, blank=True)
    codigo = models.CharField(max_length=20, null=True, blank=True)
    swift = models.CharField(max_length=20, null=True, blank=True)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "bancos"
        verbose_name = "Banco"
        verbose_name_plural = "Bancos"
    
    def __str__(self):
        return str(self.id)

class CuentaBancaria(models.Model):
    banco = models.ForeignKey(Banco, on_delete=models.CASCADE, related_name="cuentas_bancarias")
    moneda = models.ForeignKey('nucleo.Moneda', on_delete=models.CASCADE, related_name="cuentas_bancarias")
    alias = models.CharField(max_length=100, null=True, blank=True)
    titular = models.CharField(max_length=150, null=True, blank=True)
    sucursal_bancaria = models.CharField(max_length=150, null=True, blank=True)
    numero_cuenta = models.CharField(max_length=30, null=True, blank=True)
    clabe = models.CharField(max_length=30, null=True, blank=True)
    numero_cliente = models.CharField(max_length=30, null=True, blank=True)
    convenio = models.CharField(max_length=50, null=True, blank=True)
    fecha_apertura = models.DateField(auto_now=True, null=True, blank=True)
    saldo_actual = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    observaciones = models.TextField(null=True, blank=True)

    # proximamente Open Banking ---
    api_provider = models.CharField(max_length=50, null=True, blank=True)
    token = models.CharField(max_length=255, null=True, blank=True)
    refresh_token = models.CharField(max_length=255, null=True, blank=True)
    ultima_sincronizacion = models.DateTimeField(null=True, blank=True)
    # ---

    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    activo = models.BooleanField(default=True)

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
    referencia = models.CharField(max_length=100, null=True, blank=True)
    fecha_ultimo_pago = models.DateField(null=True, blank=True)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "cuentas_por_cobrar"
        verbose_name = "Cuenta Por Cobrar"
        verbose_name_plural = "Cuentas Por Cobrar"

    def __str__(self):
        return str(self.id)

class CuentaPorPagar(models.Model):
    class EstatusCxP(models.TextChoices):
        PENDIENTE = 'Pendiente', 'Pendiente'
        PARCIAL = 'Parcial', 'Parcial'
        PAGADA = 'Pagada', 'Pagada'
        CANCELADA = 'Cancelada', 'Cancelada'
        VENCIDA = 'Vencida', 'Vencida'

    proveedor = models.ForeignKey('terceros.Proveedor', on_delete=models.CASCADE, related_name="cuentas_por_pagar")
    factura_proveedor = models.ForeignKey(FacturaProveedor, on_delete=models.CASCADE, related_name="cuentas_por_pagar")
    fecha_emision = models.DateField(auto_now=True)
    fecha_vencimiento = models.DateField(null=True, blank=True)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    saldo = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    estatus = models.CharField(max_length=30, choices=EstatusCxP.choices, default=EstatusCxP.PENDIENTE)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

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

    class Estatus(models.TextChoices):
        BORRADOR = 'Borrador', 'Borrador'
        APLICADO = 'Aplicado', 'Aplicado'
        CANCELADO = 'Cancelado', 'Cancelado'

    cliente = models.ForeignKey('terceros.Cliente', on_delete=models.CASCADE, related_name="cobros")
    cuenta_bancaria = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE, related_name="cobros")

    fecha_cobro = models.DateField(auto_now=True)
    metodo_pago = models.CharField(max_length=30, choices=MetodoPago.choices, default=MetodoPago.EFECTIVO)
    referencia = models.CharField(max_length=30, choices=OpcionesReferencia.choices, default=OpcionesReferencia.SPEI)
    referencia_operacion = models.CharField(max_length=100, null=True, blank=True)

    total_cobrado = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    estatus = models.CharField(max_length=30, choices=Estatus.choices, default=Estatus.APLICADO)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    activo = models.BooleanField(default=True)

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
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "cobro_detalle"
        verbose_name = "Cobro Detalle"
        verbose_name_plural = "Cobros Detalle"

    def __str__(self):
        return str(self.id)

class Pago(models.Model):
    class Estatus(models.TextChoices):
        BORRADOR = 'Borrador', 'Borrador'
        APLICADO = 'Aplicado', 'Aplicado'
        CANCELADO = 'Cancelado', 'Cancelado'

    class MetodoPago(models.TextChoices):
        EFECTIVO = 'Efectivo', 'Efectivo'
        TRANSFERENCIA = 'Transferencia', 'Transferencia'
        TARJETA = 'Tarjeta', 'Tarjeta'
        CHEQUE = 'Cheque', 'Cheque'

    proveedor = models.ForeignKey('terceros.Proveedor', on_delete=models.CASCADE, related_name="pagos")
    cuenta_bancaria = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE, related_name="pagos")
    fecha_pago = models.DateField(auto_now=True, null=True, blank=True)
    metodo_pago = models.CharField(max_length=30, choices=MetodoPago.choices, default=MetodoPago.TRANSFERENCIA)
    referencia = models.CharField(max_length=100, null=True, blank=True)
    total_pagado = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    estatus = models.CharField(max_length=30, choices=Estatus.choices, default=Estatus.APLICADO)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "pagos"
        verbose_name = "Pago"
        verbose_name_plural = "Pagos"
    
    def __str__(self):
        return str(self.id)

class PagoDetalle(models.Model):
    pago = models.ForeignKey(Pago, on_delete=models.CASCADE, related_name="pago_detalles")
    cxp = models.ForeignKey(CuentaPorPagar, on_delete=models.CASCADE, related_name="pago_detalles")
    importe_aplicado = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)

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
    fecha = models.DateField(auto_now=True, null=True, blank=True)
    fecha_aplicacion = models.DateField(null=True, blank=True)
    concepto = models.CharField(max_length=255, null=True, blank=True)
    referencia = models.CharField(max_length=100, null=True, blank=True)
    importe = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    saldo = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class OrigenOpciones(models.TextChoices):
        MANUAL = 'Manual', 'Manual'
        SPEI = 'SPEI', 'SPEI'
        API = 'API', 'API'
        CSV = 'CSV', 'CSV'
        CONCILIACION = 'Conciliacion', 'Conciliacion'

    origen = models.CharField(max_length=20, choices=OrigenOpciones.choices, default=OrigenOpciones.MANUAL)
    
    class TipoMovimiento(models.TextChoices):
        CARGO = 'Cargo', 'Cargo'
        ABONO = 'Abono', 'Abono'

    tipo_movimiento = models.CharField(max_length=10, choices=TipoMovimiento.choices, default=TipoMovimiento.CARGO)

    class Estatus(models.TextChoices):
        PENDIENTE = 'Pendiente', 'Pendiente'
        CONCILIADO = 'Conciliado', 'Conciliado'
        CANCELADO = 'Cancelado', 'Cancelado'
    
    estatus = models.CharField(max_length=10, choices=Estatus.choices, default=Estatus.PENDIENTE)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    activo = models.BooleanField(default=True)

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
    cargo = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    abono = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    referencia = models.CharField(max_length=200, null=True, blank=True)
    observaciones = models.TextField(null=True, blank=True)
    orden = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = "poliza_detalle"
        verbose_name = "Poliza Detalle"
        verbose_name_plural = "Polizas Detalle"

    def __str__(self):
        return str(self.id)

class ConciliacionBancaria(models.Model):
    class Estatus(models.TextChoices):
        BORRADOR = 'Borrador', 'Borrador'
        CERRADA = 'Cerrada', 'Cerrada'
        CANCELADA = 'Cancelada', 'Cancelada'

    cuenta_bancaria = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE, related_name="conciliaciones_bancarias")
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_final = models.DateField(null=True, blank=True)
    saldo_estado_cuenta = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    saldo_libros = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    diferencia = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.BORRADOR)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "conciliaciones_bancarias"
        verbose_name = "Conciliacion Bancaria"
        verbose_name_plural = "Conciliaciones Bancarias"
    
    def __str__(self):
        return str(self.id)

class ConciliacionDetalle(models.Model):
    conciliacion = models.ForeignKey(ConciliacionBancaria, on_delete=models.CASCADE, related_name="conciliacion_detalles")
    movimiento_bancario = models.ForeignKey(MovimientoBancario, on_delete=models.CASCADE, related_name="conciliacion_detalles")
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)

    class Meta:
        db_table = "conciliacion_detalle"
        verbose_name = "Conciliacion Detalle"
        verbose_name_plural = "Conciliaciones Detalle"

    def __str__(self):
        return str(self.id)

class NotaCredito(models.Model):
    class Estatus(models.TextChoices):
        BORRADOR = 'Borrador', 'Borrador'
        EMITIDA = 'Emitida', 'Emitida'
        CANCELADA = 'Cancelada', 'Cancelada'

    factura = models.ForeignKey(Factura, on_delete=models.CASCADE, related_name="nota_creditos")
    cliente = models.ForeignKey('terceros.Cliente', on_delete=models.CASCADE, related_name="nota_creditos")
    fecha_emision = models.DateField(auto_now=True, null=True, blank=True)
    folio = models.CharField(max_length=30, null=True, blank=True)
    motivo = models.CharField(max_length=255, null=True, blank=True)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    impuestos = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    estatus = models.CharField(max_length=30, choices=Estatus.choices, default=Estatus.BORRADOR)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "notas_creditos"
        verbose_name = "Nota Credito"
        verbose_name_plural = "Notas Creditos"
    
    def __str__(self):
        return str(self.id)

class NotaCreditoDetalle(models.Model):
    nota_credito = models.ForeignKey(NotaCredito, on_delete=models.CASCADE, related_name="nota_credito_detalles")
    factura_detalle = models.ForeignKey(FacturaDetalle, on_delete=models.CASCADE, related_name="nota_credito_detalles")
    cantidad = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    precio_unitario = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    impuesto = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    class Meta:
        db_table = "nota_credito_detalle"
        verbose_name = "Nota Credito Detalle"
        verbose_name_plural = "Notas Creditos Detalle"
    
    def __str__(self):
        return str(self.id)
