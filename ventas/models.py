from django.db import models
from nucleo.models import Empresa, Sucursal, Moneda, StatusLifecycleModel
from terceros.models import Cliente
from catalogo.models import Producto, Talla

class Prospecto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="prospectos")
    
    class Meta:
        db_table = "prospectos"
        verbose_name = "Prospecto"
        verbose_name_plural = "Prospectos"
    
    def __str__(self):
        return str(self.id)

class Oportunidad(models.Model):
    prospecto = models.ForeignKey(Prospecto, on_delete=models.CASCADE, related_name="oportunidades")

    class Meta:
        db_table = "oportunidades"
        verbose_name = "Oportunidad"
        verbose_name_plural = "Oportunidades"
    
    def __str__(self):
        return str(self.id)

class Cotizacion(models.Model):
    class FormaPago(models.TextChoices):
        EFECTIVO = '01', '01 - Efectivo'
        TRANSFERENCIA = '03', '03 - Transferencia'
        TARJETA = '04', '04 - Tarjeta'
    
    class MetodoPago(models.TextChoices):
        PUE = 'PUE', 'PUE - Pago en una sola exhibición'
        PPD = 'PPD', 'PPD - Pago en parcialidades'
        NA = 'NA', 'N/A'
    
    class UsoCfdi(models.TextChoices):
        GO3 = 'G03', 'G03 - Gastos en general'
        GO1 = 'G01', 'G01 - Adquisición de mercancías'
        IO1 = 'I01', 'I01 - Construcciones'

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="cotizacion")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="cotizacion")
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name="cotizacion")
    oportunidad = models.ForeignKey(Oportunidad, on_delete=models.CASCADE, related_name="cotizacion", null=True)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE, related_name="cotizacion")
    # Origen
    recompra = models.BooleanField(default=False)
    chat_online = models.BooleanField(default=False)
    pedido_online = models.BooleanField(default=False)
    prospeccion = models.BooleanField(default=False)
    recomendacion = models.BooleanField(default=False)
    amazon = models.BooleanField(default=False)
    google = models.BooleanField(default=False)
    publicidad = models.BooleanField(default=False)
    mercado_libre = models.BooleanField(default=False)
    redes_sociales = models.BooleanField(default=False)
    otro = models.BooleanField(default=False)
    mailing = models.BooleanField(default=False)
    # Forma de pago y contacto para envio de facturas
    persona_pagos = models.CharField(max_length=100)
    correo_facturas = models.EmailField(max_length=150)
    telefono_pagos = models.CharField(max_length=20)
    oc = models.CharField(max_length=100, null=True)
    forma_pago = models.CharField(max_length=5, choices=FormaPago.choices)
    metodo_pago = models.CharField(max_length=10, choices=MetodoPago.choices)
    uso_cfdi = models.CharField(max_length=10, choices=UsoCfdi.choices)
    # Condiciones de pago
    anticipo_total = models.BooleanField(default=False)
    anticipo_parcial = models.BooleanField(default=False)
    vendedor_autoriza = models.BooleanField(default=False)
    pago_antes_embarque = models.BooleanField(default=False)
    por_confirmar = models.BooleanField(default=False)
    otra_cantidad = models.BooleanField(default=False)
    monto = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Envio
    empaque_ecologico = models.BooleanField(default=False)
    embarque_parcial = models.BooleanField(default=False)
    comentarios_parcialidad = models.TextField(null=True, blank=True)
    # Servicios extra
    envio = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    programa_bordados = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bordado_pantalones_extras = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bordado_logotipo = models.BooleanField(default=False)
    observaciones = models.TextField(null=True, blank=True)
    # Cargos adicionales
    flete = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    seguros = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    anticipo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    descuento_global = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    ieps = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    iva = models.IntegerField(default=16)
    gran_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

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

class Pedido(StatusLifecycleModel):
    class FormaPago(models.TextChoices):
        EFECTIVO = '01', '01 - Efectivo'
        TRANSFERENCIA = '03', '03 - Transferencia'
        TARJETA = '04', '04 - Tarjeta'
    
    class MetodoPago(models.TextChoices):
        PUE = 'PUE', 'PUE - Pago en una sola exhibición'
        PPD = 'PPD', 'PPD - Pago en parcialidades'
        NA = 'NA', 'N/A'
    
    class UsoCfdi(models.TextChoices):
        GO3 = 'G03', 'G03 - Gastos en general'
        GO1 = 'G01', 'G01 - Adquisición de mercancías'
        IO1 = 'I01', 'I01 - Construcciones'

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
    cotizacion = models.ForeignKey(Cotizacion, on_delete=models.CASCADE, related_name="pedidos", null=True, blank=True)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE, related_name="pedidos")
    tipo_pedido = models.SmallIntegerField(default=1, choices=CHOICES_TIPO_PEDIDO)
    estatus = models.SmallIntegerField(default=1, choices=CHOICES_ESTATUS)
    # Origen
    recompra = models.BooleanField(default=False)
    chat_online = models.BooleanField(default=False)
    pedido_online = models.BooleanField(default=False)
    prospeccion = models.BooleanField(default=False)
    recomendacion = models.BooleanField(default=False)
    amazon = models.BooleanField(default=False)
    google = models.BooleanField(default=False)
    publicidad = models.BooleanField(default=False)
    mercado_libre = models.BooleanField(default=False)
    redes_sociales = models.BooleanField(default=False)
    otro = models.BooleanField(default=False)
    mailing = models.BooleanField(default=False)
    # Forma de pago y contacto para envio de facturas
    persona_pagos = models.CharField(max_length=100)
    correo_facturas = models.EmailField(max_length=150)
    telefono_pagos = models.CharField(max_length=20)
    oc = models.CharField(max_length=100, null=True)
    forma_pago = models.CharField(max_length=5, choices=FormaPago.choices)
    metodo_pago = models.CharField(max_length=10, choices=MetodoPago.choices)
    uso_cfdi = models.CharField(max_length=10, choices=UsoCfdi.choices)
    # Condiciones de pago
    anticipo_total = models.BooleanField(default=False)
    anticipo_parcial = models.BooleanField(default=False)
    vendedor_autoriza = models.BooleanField(default=False)
    pago_antes_embarque = models.BooleanField(default=False)
    por_confirmar = models.BooleanField(default=False)
    otra_cantidad = models.BooleanField(default=False)
    monto = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Envio
    empaque_ecologico = models.BooleanField(default=False)
    embarque_parcial = models.BooleanField(default=False)
    comentarios_parcialidad = models.TextField(null=True, blank=True)
    # Servicios extra
    envio = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    programa_bordados = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bordado_pantalones_extras = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bordado_logotipo = models.BooleanField(default=False)
    observaciones = models.TextField(null=True, blank=True)
    # Cargos adicionales
    flete = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    seguros = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    anticipo = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    descuento_global = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    ieps = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    iva = models.IntegerField(default=16)
    gran_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
    fecha_confirmacion = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "pedidos"
        verbose_name = "Pedido"
        verbose_name_plural = "Pedidos"
    
    def __str__(self):
        return str(self.id)
    
class PedidoDetalle(models.Model):
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="detalles")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="detalles")
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    costo_unitario = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    subtotal_linea = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        db_table = "pedido_detalle"
        verbose_name = "Pedido Detalle"
        verbose_name_plural = "Pedidos Detalle"
    
    def __str__(self):
        return str(self.id)

class PedidoDetalleTalla(models.Model):
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE, related_name="tallas")
    talla = models.ForeignKey(Talla, on_delete=models.PROTECT, related_name="pedido_detalles_talla")
    cantidad = models.PositiveIntegerField(default=1)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    subtotal_talla = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        db_table = "pedido_detalle_talla"
        verbose_name = "Pedido Detalle Talla"
        verbose_name_plural = "Pedidos Detalle Tallas"
    
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
