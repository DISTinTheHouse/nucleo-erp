from django.db import models
from django.conf import settings
from nucleo.models import Empresa, Sucursal, Moneda, SerieFolio, StatusLifecycleModel, SatRegimenFiscal
from terceros.models import Cliente, DireccionCliente
from catalogo.models import Producto, Talla, Color

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
        EFECTIVO = '01', '01 - EFECTIVO'
        TRANSFERENCIA = '03', '03 - TRANSFERENCIA'
        TARJETA = '04', '04 - TARJETA'

    class MetodoPago(models.TextChoices):
        PUE = 'PUE', 'PUE - PAGO EN UNA SOLA EXIBICIÓN'
        PPD = 'PPD', 'PPD - PAGO EN PARCIALIDADES'
        NA = 'NA', 'N/A'

    class UsoCfdi(models.TextChoices):
        GO3 = 'G03', 'G03 - GASTOS EN GENERAL (OTROS) (OTROS)'
        GO1 = 'G01', 'G01 - ADQUISICIÓN DE MERCANCIAS'
        IO1 = 'I01', 'I01 - CONSTRUCCIONES'

    CHOICES_ESTATUS = (
        (1, "BORRADOR"),
        (2, "ENVIAR A AUTORIZACION"),
        (3, "EN REVISION"),
        (4, "RECHAZADA"),
        (5, "CAMBIOS SOLICITADOS"),
        (6, "AUTORIZADA"),
        # cotizacion finalizada
    )

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="cotizacion")
    vendedor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="cotizaciones", null=True, blank=True, db_index=True)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="cotizacion")
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, related_name="cotizacion")
    oportunidad = models.ForeignKey(Oportunidad, on_delete=models.CASCADE, related_name="cotizacion", null=True)
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE, related_name="cotizacion")
    estatus = models.SmallIntegerField(default=2, choices=CHOICES_ESTATUS, db_index=True)
    autorizada_at = models.DateTimeField(null=True, blank=True)
    cambios_solicitados_at = models.DateTimeField(null=True, blank=True)
    aprobado_snapshot = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)
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
    oc = models.CharField(max_length=100, null=True, blank=True)
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
    destinatario = models.CharField(max_length=150, null=True, blank=True)
    empresa_envio = models.CharField(max_length=150, null=True, blank=True)
    telefono_envio = models.CharField(max_length=20, null=True, blank=True)
    celular_envio = models.CharField(max_length=20, null=True, blank=True)
    direccion_envio = models.CharField(max_length=255, null=True, blank=True)
    colonia_envio = models.CharField(max_length=120, null=True, blank=True)
    codigo_postal = models.CharField(max_length=10, null=True, blank=True)
    ciudad_envio = models.CharField(max_length=120, null=True, blank=True)
    estado_envio = models.CharField(max_length=120, null=True, blank=True)
    referencias = models.TextField(null=True, blank=True)
    # Servicios extra
    envio = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    programa_bordados = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bordado_pantalones_extras = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bordado_logotipo = models.BooleanField(default=False)
    serigrafia = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    reflejante = models.DecimalField(max_digits=10, decimal_places=2, default=0)
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
    color = models.ForeignKey(Color, on_delete=models.PROTECT, related_name="cotizacion_detalles", null=True, blank=True)
    direccion_envio_cliente = models.ForeignKey(
        DireccionCliente, on_delete=models.PROTECT, related_name="cotizacion_detalles", null=True, blank=True
    )
    precio_lista = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    costo_unitario = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    subtotal_linea = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        db_table = "cotizacion_detalle"
        verbose_name = "Cotización Detalle"
        verbose_name_plural = "Cotizaciones Detalle"
    
    def __str__(self):
        return str(self.id)

class CotizacionDetalleTalla(models.Model):
    cotizacion_detalle = models.ForeignKey(CotizacionDetalle, on_delete=models.CASCADE, related_name="tallas")
    talla = models.ForeignKey(Talla, on_delete=models.PROTECT, related_name="cotizacion_detalles_talla")
    cantidad = models.PositiveIntegerField(default=1)
    precio_unitario = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    subtotal_talla = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    lleva_bordado = models.BooleanField(default=False)
    bordado_config = models.JSONField(null=True, blank=True)
    lleva_reflejante = models.BooleanField(default=False)
    reflejante_config = models.JSONField(null=True, blank=True)
    lleva_corte_manga = models.BooleanField(default=False)
    corte_manga_config = models.JSONField(null=True, blank=True)
    lleva_cambio_talla = models.BooleanField(default=False)
    cambio_talla_config = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "cotizacion_detalle_talla"
        verbose_name = "Cotización Detalle Talla"
        verbose_name_plural = "Cotizaciones Detalle Tallas"
    
    def __str__(self):
        return str(self.id)

class CotizacionServicioExtra(models.Model):
    cotizacion = models.ForeignKey(Cotizacion, on_delete=models.CASCADE, related_name="servicios_extras")
    nombre = models.CharField(max_length=150)
    monto = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    visible_en_factura = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "cotizacion_servicios_extras"
        verbose_name = "Cotización Servicio Extra"
        verbose_name_plural = "Cotizaciones Servicios Extras"

    def __str__(self):
        return str(self.id)

class Pedido(StatusLifecycleModel):
    class FormaPago(models.TextChoices):
        EFECTIVO = '01', '01 - EFECTIVO'
        TRANSFERENCIA = '03', '03 - TRANSFERENCIA'
        TARJETA = '04', '04 - TARJETA'
    
    class MetodoPago(models.TextChoices):
        PUE = 'PUE', 'PUE - PAGO EN UNA SOLA EXIBICIÓN'
        PPD = 'PPD', 'PPD - PAGO EN PARCIALIDADES'
        NA = 'NA', 'N/A'
    
    class UsoCfdi(models.TextChoices):
        GO3 = 'G03', 'G03 - GASTOS EN GENERAL (OTROS) (OTROS)'
        GO1 = 'G01', 'G01 - ADQUISICIÓN DE MERCANCIAS'
        IO1 = 'I01', 'I01 - CONSTRUCCIONES'

    CHOICES_ESTATUS = (
        (1, "BORRADOR"),
        (2, "POR AUTORIZAR"),
        (3, "AUTORIZADA"),
        (4, "EN PROCESO"),
        (5, "CANCELADO"),
    )

    CHOICES_TIPO_PEDIDO = (
        (1, "PEDIDO DE VENTA"),
        (2, "MUESTRA"),
        (3, "PEDIDO DE ERROR"),
    )
        
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="pedidos")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="pedidos")
    serie_folio = models.ForeignKey(SerieFolio, on_delete=models.PROTECT, related_name="pedidos", null=True, blank=True)
    folio = models.CharField(max_length=50, null=True, blank=True, db_index=True)
    folio_consecutivo = models.PositiveIntegerField(null=True, blank=True)
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
    oc = models.CharField(max_length=100, null=True, blank=True)
    forma_pago = models.CharField(max_length=5, choices=FormaPago.choices)
    metodo_pago = models.CharField(max_length=10, choices=MetodoPago.choices)
    uso_cfdi = models.CharField(max_length=10, choices=UsoCfdi.choices)
    cliente_razon_social = models.CharField(max_length=255, null=True, blank=True)
    cliente_nombre = models.CharField(max_length=150, null=True, blank=True)
    cliente_rfc = models.CharField(max_length=13, null=True, blank=True)
    cliente_regimen_fiscal = models.ForeignKey(SatRegimenFiscal, on_delete=models.PROTECT, related_name="pedidos", null=True, blank=True)
    cliente_direccion_fiscal = models.CharField(max_length=255, null=True, blank=True)
    cliente_colonia = models.CharField(max_length=120, null=True, blank=True)
    cliente_codigo_postal = models.CharField(max_length=10, null=True, blank=True)
    cliente_ciudad = models.CharField(max_length=120, null=True, blank=True)
    cliente_estado = models.CharField(max_length=120, null=True, blank=True)
    cliente_giro_empresarial = models.CharField(max_length=150, null=True, blank=True)
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
    destinatario = models.CharField(max_length=150, null=True, blank=True)
    empresa_envio = models.CharField(max_length=150, null=True, blank=True)
    telefono_envio = models.CharField(max_length=20, null=True, blank=True)
    celular_envio = models.CharField(max_length=20, null=True, blank=True)
    direccion_envio = models.CharField(max_length=255, null=True, blank=True)
    colonia_envio = models.CharField(max_length=120, null=True, blank=True)
    codigo_postal = models.CharField(max_length=10, null=True, blank=True)
    ciudad_envio = models.CharField(max_length=120, null=True, blank=True)
    estado_envio = models.CharField(max_length=120, null=True, blank=True)
    referencias = models.TextField(null=True, blank=True)
    # Servicios extra
    envio = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    programa_bordados = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bordado_pantalones_extras = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bordado_logotipo = models.BooleanField(default=False)
    serigrafia = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    reflejante = models.DecimalField(max_digits=10, decimal_places=2, default=0)
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
        constraints = [
            models.UniqueConstraint(fields=["serie_folio", "folio"], name="uq_pedido_seriefolio_folio")
        ]
    
    def __str__(self):
        return str(self.id)

class PedidoServicioExtra(models.Model):
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="servicios_extras")
    nombre = models.CharField(max_length=150)
    monto = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    visible_en_factura = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "pedido_servicios_extras"
        verbose_name = "Pedido Servicio Extra"
        verbose_name_plural = "Pedidos Servicios Extras"

    def __str__(self):
        return str(self.id)
    
class PedidoDetalle(models.Model):
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="detalles")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="detalles")
    color = models.ForeignKey(Color, on_delete=models.PROTECT, related_name="pedido_detalles", null=True, blank=True)
    direccion_envio_cliente = models.ForeignKey(
        DireccionCliente, on_delete=models.PROTECT, related_name="pedido_detalles", null=True, blank=True
    )
    precio_lista = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
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
    lleva_bordado = models.BooleanField(default=False)
    bordado_config = models.JSONField(null=True, blank=True)
    lleva_reflejante = models.BooleanField(default=False)
    reflejante_config = models.JSONField(null=True, blank=True)
    lleva_corte_manga = models.BooleanField(default=False)
    corte_manga_config = models.JSONField(null=True, blank=True)
    lleva_cambio_talla = models.BooleanField(default=False)
    cambio_talla_config = models.JSONField(null=True, blank=True)

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
