from django.db import models
from django.conf import settings
from nucleo.models import Empresa, Sucursal, Departamento, Moneda, StatusLifecycleModel
from terceros.models import Proveedor, Transportista
from catalogo.models import Producto, ProductoVariante, VarianteProductoProduccion
from ventas.models import Pedido

from inventarios.models import Almacen, Ubicacion, Lote, Serie
from produccion.models import OrdenProduccion, OrdenProduccionDetalle

from simple_history.models import HistoricalRecords


class Requisicion(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    departamento = models.ForeignKey(Departamento, on_delete=models.CASCADE)

    history = HistoricalRecords()

    class Meta:
        db_table = 'requisiciones'
        verbose_name = 'Requisición'
        verbose_name_plural = 'Requisiciones'

    def __str__(self):
        return str(self.id)

class RequisicionDetalle(models.Model):
    requisicion = models.ForeignKey(Requisicion, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)

    history = HistoricalRecords()

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

    history = HistoricalRecords()

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

    history = HistoricalRecords()

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

    history = HistoricalRecords()

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

    history = HistoricalRecords()

    class Meta:
        db_table = 'solicitud_compra_detalle'
        verbose_name = 'Solicitud Compra Detalle'
        verbose_name_plural = 'Solicitudes Compra Detalle'

    def __str__(self):
        return str(self.id)

class OrdenCompra(StatusLifecycleModel):
    class EstatusOrdenCompra(models.IntegerChoices):
        BORRADOR = 1, 'Borrador'
        POR_AUTORIZAR = 2, 'Pendiente a confirmar'
        AUTORIZADA = 3, 'Autorizada'
        PARCIALMENTE_RECIBIDA = 4, 'Parcialmente recibida'
        RECIBIDA = 5, 'Recibida'
        CANCELADA = 6, 'Cancelada'

    class TipoCompra(models.TextChoices):
        # Decide la serie de folio al aceptar: MP -> ``ORDEN_COMPRA`` (serie OC),
        # PT -> ``ORDEN_COMPRA_PT`` (serie O).
        MATERIA_PRIMA = 'MP', 'Materia prima'
        PRODUCTO_TERMINADO = 'PT', 'Producto terminado'

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    # Almacén al que entra lo que se recibe contra esta OC (WMS lo preselecciona).
    almacen = models.ForeignKey(
        'inventarios.Almacen', on_delete=models.SET_NULL, null=True, blank=True, related_name='ordenes_compra',
    )
    proveedor = models.ForeignKey(Proveedor, on_delete=models.SET_NULL, null=True, blank=True)
    solicitud_compra = models.ForeignKey(SolicitudCompra, on_delete=models.SET_NULL, null=True, blank=True)
    # OC en Borrador que generó una pre-orden de producción con faltantes
    # (``pre-ordenes/{id}/confirmar-existencias``); al recibirla se recalcula.
    pre_orden = models.ForeignKey(
        'produccion.PreOrdenProduccion', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ordenes_compra',
    )
    moneda = models.ForeignKey(Moneda, on_delete=models.CASCADE)
    # Pesos por unidad de ``moneda``; obligatorio si no es MXN. Convierte el
    # precio de la OC al costo en pesos del inventario que se recibe (#809).
    tipo_cambio = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.SET_NULL, related_name='ordenes_compra', null=True, blank=True)
    # Una OC puede surtir varios pedidos. ``pedido`` (FK) se conserva por
    # compatibilidad y siempre refleja el primero de ``pedidos``.
    pedidos = models.ManyToManyField(Pedido, related_name='ordenes_compra_vinculadas', blank=True)
    tipo_compra = models.CharField(max_length=2, choices=TipoCompra.choices, default=TipoCompra.MATERIA_PRIMA)

    folio = models.CharField(max_length=30, unique=True, null=True, blank=True)
    referencia = models.CharField(max_length=50, null=True, blank=True)

    fecha_oc = models.DateField()
    fecha_entrega_estimada = models.DateField(null=True)
    fecha_autorizacion = models.DateTimeField(null=True)
    fecha_vencimiento = models.DateField(null=True)
    
    estatus = models.IntegerField(choices=EstatusOrdenCompra.choices, default=EstatusOrdenCompra.BORRADOR)

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
    # Solo lo escribe ``POST ordenes/{id}/cancelar/``. Nulo en las OC que ya
    # estaban en CANCELADA (puestas desde el admin) antes de existir la acción.
    motivo_cancelacion = models.TextField(blank=True, null=True)
    # ``POST ordenes/{id}/cerrar-saldo/``: la OC parcial se da por terminada (el
    # proveedor ya no surte el resto). Queda RECIBIDA aunque falten piezas.
    saldo_cerrado = models.BooleanField(default=False)
    motivo_cierre = models.TextField(blank=True, default='')
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    cerrada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    # ``POST pedidos/{id}/cancelar/`` con la OC ya autorizada: no se cancela sola,
    # queda marcada para que compras decida (cancelarla o quedarse el material).
    pedido_cancelado = models.BooleanField(default=False)
    fecha_pedido_cancelado = models.DateTimeField(null=True, blank=True)

    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        db_table = 'ordenes_compra'
        verbose_name = 'Orden Compra'
        verbose_name_plural = 'Ordenes Compra'

    def __str__(self):
        return str(self.id)

class OrdenCompraRutaCritica(models.Model):
    """Seguimiento informativo de la OC: fecha en que compras supo que llegó la
    mercancía y un comentario. No afecta la OC, las recepciones ni WMS (que sigue
    haciendo la recepción real). Quién y qué cambió queda en ``AuditoriaEvento``."""

    oc = models.OneToOneField(
        OrdenCompra, on_delete=models.CASCADE, primary_key=True, related_name="ruta_critica"
    )
    fecha_real_recibo = models.DateField(null=True, blank=True)
    comentarios = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ordenes_compra_ruta_critica'
        verbose_name = 'Ruta Crítica de Orden de Compra'
        verbose_name_plural = 'Rutas Críticas de Órdenes de Compra'

    def __str__(self):
        return f"Ruta crítica OC {self.oc_id}"


class NecesidadCompra(models.Model):
    """Material que le falta a una pre-orden de producción (al confirmar
    existencias). Compras las agrupa por material (``necesidades/agrupadas``) y
    las consolida en una OC (``ordenes/desde-necesidades``); al recibir la OC lo
    recibido se reparte entre ellas por ``fecha_requerida``.
    """

    class Estatus(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        EN_OC = "en_oc", "En OC"
        RECIBIDA = "recibida", "Recibida"
        HUERFANA = "huerfana", "Huérfana"     # pre-orden cancelada con la necesidad ya en una OC
        CANCELADA = "cancelada", "Cancelada"  # pre-orden cancelada antes de pasar a OC

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT)
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name='necesidades_compra')
    unidad = models.ForeignKey('nucleo.UnidadMedida', on_delete=models.SET_NULL, null=True, blank=True)
    cantidad = models.DecimalField(max_digits=14, decimal_places=4)
    cantidad_recibida = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    # Lo que ya no hace falta (BOM editado con la necesidad en una OC): la OC no se toca.
    sobrante = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    pre_orden = models.ForeignKey('produccion.PreOrdenProduccion', on_delete=models.CASCADE, related_name='necesidades')
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name='necesidades_compra', null=True, blank=True)
    # Compra directa de un SKU de resurtido (sin BOM): su variante y su renglón.
    producto_variante = models.ForeignKey('catalogo.ProductoVariante', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    pre_orden_detalle = models.ForeignKey('produccion.PreOrdenProduccionDetalle', on_delete=models.CASCADE, null=True, blank=True, related_name='necesidades')
    fecha_requerida = models.DateField(null=True, blank=True)
    estatus = models.CharField(max_length=10, choices=Estatus.choices, default=Estatus.PENDIENTE, db_index=True)
    orden_compra = models.ForeignKey('OrdenCompra', on_delete=models.SET_NULL, null=True, blank=True, related_name='necesidades')
    orden_compra_detalle = models.ForeignKey('OrdenCompraDetalle', on_delete=models.SET_NULL, null=True, blank=True, related_name='necesidades')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'necesidades_compra'
        verbose_name = 'Necesidad de compra'
        verbose_name_plural = 'Necesidades de compra'
        indexes = [models.Index(fields=['empresa', 'estatus', 'producto'], name='ix_necesidad_emp_est_prod')]

    def __str__(self):
        return f"Necesidad {self.pk} ({self.producto_id})"


class OrdenCompraDetalle(models.Model):
    orden_compra = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    # SKU comprado (OC de Producto Terminado); pasa tal cual a la recepción.
    producto_variante = models.ForeignKey(
        'catalogo.ProductoVariante', on_delete=models.PROTECT, null=True, blank=True
    )
    solicitud_compra_detalle = models.ForeignKey(SolicitudCompraDetalle, on_delete=models.CASCADE, null=True, blank=True)
    requisicion_detalle = models.ForeignKey(RequisicionDetalle, on_delete=models.CASCADE, null=True, blank=True)
    descripcion = models.CharField(max_length=200, null=True, blank=True)
    cantidad = models.IntegerField(default=0)
    precio = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    descuento = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    importe = models.DecimalField(max_digits=14, decimal_places=2, default=0.00)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE, related_name='ordenes_compra_detalle')
    piezas = models.IntegerField(default=0)

    history = HistoricalRecords()

    class Meta:
        db_table = 'orden_compra_detalle'
        verbose_name = 'Orden Compra Detalle'
        verbose_name_plural = 'Ordenes Compra Detalle'

    def __str__(self):
        return str(self.id)

class Recepcion(models.Model):
    class TipoOrigen(models.TextChoices):
        ORDEN_COMPRA = "OC", "Orden de compra"
        ORDEN_PRODUCCION = "OP", "Orden de produccion"
        # Regreso de una salida a maquila de OB/OR: sin movimiento de inventario.
        SALIDA_MAQUILA = "SM", "Salida a maquila"

    class EstatusRecepcion(models.IntegerChoices):
        BORRADOR = 1, 'Borrador'
        RECIBIDA = 2, 'Recibida'
        PARCIAL = 3, 'Parcial'
        EN_CALIDAD = 4, 'En calidad'
        CERRADA = 5, 'Cerrada'
        CANCELADA = 6, 'Cancelada'
    
    tipo_origen = models.CharField(
        max_length=2,
        choices=TipoOrigen.choices,
        default=TipoOrigen.ORDEN_COMPRA,
    )
    orden_compra = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE, null=True, blank=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE, null=True, blank=True)
    # SM que regresa del maquilero (con la OP, o sola si viene de OB/OR).
    salida_maquila = models.ForeignKey(
        "produccion.SalidaMaquila", on_delete=models.PROTECT, null=True, blank=True, related_name="recepciones"
    )
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name='recepciones')
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE, related_name='recepciones')
    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE, related_name='recepciones', null=True, blank=True)
    almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE)
    transportista = models.ForeignKey(Transportista, on_delete=models.CASCADE, related_name='recepciones', null=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recepciones')

    folio = models.CharField(max_length=30, unique=True)
    remision = models.CharField(max_length=50, null=True)
    factura_referencia = models.CharField(max_length=50, null=True)

    fecha_recepcion = models.DateTimeField()

    estatus = models.IntegerField(choices=EstatusRecepcion.choices, default=EstatusRecepcion.BORRADOR)

    class AlcanceRecepcion(models.TextChoices):
        PARCIAL = "PARCIAL", "Parcial"
        COMPLETA = "COMPLETA", "Completa"

    # Al registrarla: ¿con ella ya no le quedó pendiente a la orden (OC, OP o SM)?
    # Se guarda tal cual aunque otra recepción complete la orden después. Nulo en
    # recepciones anteriores a este campo.
    alcance_recepcion = models.CharField(max_length=10, choices=AlcanceRecepcion.choices, null=True, blank=True)
    activo = models.BooleanField(default=True)

    observaciones = models.TextField(blank=True, null=True)

    # Solo los escribe ``POST recepciones/{id}/cancelar/`` (EC-926). Nulos en las
    # recepciones no canceladas y en las canceladas antes de existir los campos
    # sin evento ``CANCELAR`` en auditoría.
    motivo_cancelacion = models.CharField(max_length=500, null=True, blank=True)
    fecha_cancelacion = models.DateTimeField(null=True, blank=True)
    cancelado_por = models.ForeignKey(
        # SET_NULL como ``AuditoriaEvento.usuario``: borrar al usuario no se bloquea.
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='recepciones_canceladas',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        db_table = 'recepciones'
        verbose_name = 'Recepcion'
        verbose_name_plural = 'Recepciones'

    def __str__(self):
        return str(self.id)

class RecepcionDetalle(models.Model):
    recepcion = models.ForeignKey(Recepcion, on_delete=models.CASCADE)
    orden_compra_detalle = models.ForeignKey(OrdenCompraDetalle, on_delete=models.CASCADE, null=True, blank=True)
    orden_produccion_detalle = models.ForeignKey(OrdenProduccionDetalle, on_delete=models.CASCADE, null=True, blank=True)
    # Catálogo: ``producto`` (+ ``producto_variante``). Muestra de fabricación
    # especial (renglón de OP con ``variante_produccion``): solo
    # ``variante_produccion``, como su ``Existencia``.
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, null=True, blank=True)
    producto_variante = models.ForeignKey(ProductoVariante, on_delete=models.CASCADE, null=True, blank=True)
    variante_produccion = models.ForeignKey(
        VarianteProductoProduccion, on_delete=models.PROTECT, null=True, blank=True,
        related_name="recepcion_detalles",
    )
    # Regreso de una SM de OB/OR: lo que volvió de cada renglón de la SM. Solo
    # cuenta piezas; no entra al inventario (las prendas ya son del pedido).
    salida_maquila_detalle = models.ForeignKey(
        "produccion.SalidaMaquilaDetalle", on_delete=models.PROTECT, null=True, blank=True,
        related_name="recepcion_detalles",
    )
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.CASCADE, null=True, blank=True)
    lote = models.ForeignKey(Lote, on_delete=models.CASCADE, null=True, blank=True)
    serie = models.ForeignKey(Serie, on_delete=models.CASCADE, null=True, blank=True)
    # Sin default: un renglón siempre trae lo recibido, y 0 lo rechaza la BD.
    cantidad_recibida = models.DecimalField(max_digits=18, decimal_places=4)

    history = HistoricalRecords()

    class Meta:
        db_table = 'recepcion_detalle'
        verbose_name = 'Recepcion Detalle'
        verbose_name_plural = 'Recepciones Detalle'
        constraints = [
            # Un renglón de recepción registra algo recibido (EC-894).
            models.CheckConstraint(
                condition=models.Q(cantidad_recibida__gt=0),
                name="ck_recepcion_detalle_cantidad_recibida_positiva",
                violation_error_message="La cantidad recibida debe ser mayor a 0.",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(producto__isnull=False)
                    | models.Q(variante_produccion__isnull=False)
                    | models.Q(salida_maquila_detalle__isnull=False)
                ),
                name="ck_recepcion_detalle_articulo",
                violation_error_message="El renglón requiere producto, variante de producción o renglón de SM.",
            ),
        ]

    def __str__(self):
        return str(self.id)

    # Nombre/SKU/talla del artículo, sea de catálogo o muestra. Quien los use
    # trae ``producto``, ``producto_variante__talla`` y
    # ``variante_produccion__talla`` en su ``select_related``.
    @property
    def articulo_nombre(self):
        if self.producto_id:
            return self.producto.nombre
        return (
            getattr(self.variante_produccion, "nombre", None)
            or getattr(self.variante_produccion, "sku", None)
            or getattr(self.salida_maquila_detalle, "descripcion", None)
        )

    @property
    def articulo_sku(self):
        return getattr(self.producto_variante, "sku", None) or getattr(self.variante_produccion, "sku", None)

    @property
    def articulo_talla(self):
        variante = self.producto_variante or self.variante_produccion
        return getattr(getattr(variante, "talla", None), "nombre", None)


class RecepcionRFIDEncuadre(models.Model):
    class Estatus(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        ACEPTADO = "ACEPTADO", "Aceptado"
        CANCELADO = "CANCELADO", "Cancelado"

    tipo_origen = models.CharField(
        max_length=2,
        choices=Recepcion.TipoOrigen.choices,
        default=Recepcion.TipoOrigen.ORDEN_COMPRA,
    )
    orden_compra = models.ForeignKey(
        OrdenCompra,
        on_delete=models.CASCADE,
        related_name="rfid_encuadres",
        null=True,
        blank=True,
    )
    op = models.ForeignKey(
        OrdenProduccion,
        on_delete=models.CASCADE,
        related_name="rfid_encuadres",
        null=True,
        blank=True,
    )
    recepcion = models.ForeignKey(
        Recepcion,
        on_delete=models.SET_NULL,
        related_name="rfid_encuadres",
        null=True,
        blank=True,
    )
    empresa = models.ForeignKey(
        Empresa,
        on_delete=models.CASCADE,
        related_name="rfid_encuadres_recepcion",
    )
    sucursal = models.ForeignKey(
        Sucursal,
        on_delete=models.CASCADE,
        related_name="rfid_encuadres_recepcion",
    )
    proveedor = models.ForeignKey(
        Proveedor,
        on_delete=models.CASCADE,
        related_name="rfid_encuadres_recepcion",
        null=True,
        blank=True,
    )
    almacen = models.ForeignKey(
        Almacen,
        on_delete=models.CASCADE,
        related_name="rfid_encuadres_recepcion",
    )
    transportista = models.ForeignKey(
        Transportista,
        on_delete=models.CASCADE,
        related_name="rfid_encuadres_recepcion",
        null=True,
        blank=True,
    )
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="rfid_encuadres_recepcion",
    )
    serie_codigo = models.CharField(max_length=2, default="RC")
    fecha_recepcion = models.DateTimeField(null=True, blank=True)
    remision = models.CharField(max_length=50, null=True, blank=True)
    factura_referencia = models.CharField(max_length=50, null=True, blank=True)
    observaciones = models.TextField(blank=True, null=True)
    estatus = models.CharField(
        max_length=20,
        choices=Estatus.choices,
        default=Estatus.PENDIENTE,
    )
    aceptado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    fecha_aceptacion = models.DateTimeField(null=True, blank=True)
    motivo_cancelacion = models.TextField(blank=True, default='')
    fecha_cancelacion = models.DateTimeField(null=True, blank=True)
    cancelado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "recepcion_rfid_encuadre"
        verbose_name = "Recepcion RFID Encuadre"
        verbose_name_plural = "Recepciones RFID Encuadre"

    def __str__(self):
        return f"Encuadre RFID {self.pk}"


class RecepcionRFIDLectura(models.Model):
    encuadre = models.ForeignKey(
        RecepcionRFIDEncuadre,
        on_delete=models.CASCADE,
        related_name="lecturas",
    )
    codigo_tag = models.CharField(max_length=120)
    orden_compra_detalle = models.ForeignKey(
        OrdenCompraDetalle,
        on_delete=models.CASCADE,
        related_name="rfid_lecturas_recepcion",
        null=True,
        blank=True,
    )
    orden_produccion_detalle = models.ForeignKey(
        OrdenProduccionDetalle,
        on_delete=models.CASCADE,
        related_name="rfid_lecturas_recepcion",
        null=True,
        blank=True,
    )
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, null=True, blank=True)
    producto_variante = models.ForeignKey(
        ProductoVariante,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.CASCADE, null=True, blank=True)
    lote = models.ForeignKey(Lote, on_delete=models.CASCADE, null=True, blank=True)
    serie = models.ForeignKey(Serie, on_delete=models.CASCADE, null=True, blank=True)
    cantidad_leida = models.DecimalField(max_digits=18, decimal_places=4, default=1)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recepcion_rfid_lectura"
        verbose_name = "Recepcion RFID Lectura"
        verbose_name_plural = "Recepciones RFID Lecturas"
        constraints = [
            models.UniqueConstraint(
                fields=("encuadre", "codigo_tag"),
                name="uq_recepcion_rfid_lectura_encuadre_tag",
            )
        ]

    def __str__(self):
        return self.codigo_tag

class CalidadInspeccion(models.Model):
    ESTADO_ANULADA = 'anulada'
    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('aprobada', 'Aprobada'),
        ('rechazada', 'Rechazada'),
        ('aprobada_condicion', 'Aprobada con condiciones'),
        # ``POST calidad-inspecciones/{id}/revertir/``: se sacó del inventario lo que
        # liberó; sus detalles ya no cuentan como inspeccionados.
        ('anulada', 'Anulada'),
    ]

    recepcion = models.ForeignKey(Recepcion, on_delete=models.CASCADE, related_name='calidad_inspecciones')
    inspector = models.ForeignKey('hr.Empleado', on_delete=models.PROTECT, related_name='calidad_inspecciones')
    fecha = models.DateField()
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    observaciones = models.TextField(blank=True, null=True)
    motivo_anulacion = models.TextField(blank=True, default='')
    fecha_anulacion = models.DateTimeField(null=True, blank=True)
    anulada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    # La bitácora (``auditoria/bitacora.py``) acota por empresa/sucursal: las de su recepción.
    @property
    def empresa_id(self):
        return self.recepcion.empresa_id

    @property
    def sucursal_id(self):
        return self.recepcion.sucursal_id

    class Meta:
        db_table = 'calidad_inspecciones'
        verbose_name = 'Calidad Inspección'
        verbose_name_plural = 'Calidad Inspecciones'

    def __str__(self):
        return str(self.id)

class CalidadInspeccionDetalle(models.Model):
    RESULTADO_CHOICES = [
        ('cuarentena', 'Cuarentena'),
        ('liberado', 'Liberado'),
        ('concesion_cc', 'Concesión Control de Calidad'),
        ('concesion lazzar', 'Concesión lazzar'),
        ('rechazo', 'Rechazo'),
    ]

    calidad_inspeccion = models.ForeignKey(CalidadInspeccion, on_delete=models.CASCADE, related_name='detalles')
    recepcion_detalle = models.ForeignKey(RecepcionDetalle, on_delete=models.CASCADE, related_name='calidad_inspecciones')
    cantidad_inspeccionada = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cantidad_aprobada = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cantidad_rechazada = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    resultado = models.TextField(max_length=20, choices=RESULTADO_CHOICES, default='aprobado')
    motivo_rechazo = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'calidad_inspeccion_detalle'
        verbose_name = 'Calidad Inspección Detalle'
        verbose_name_plural = 'Calidad Inspecciones Detalle'

    def __str__(self):
        return str(self.id)

class EnvioProveedor(models.Model):
    ESTADO_CHOICES = [
        ('preparando', 'Preparando'),
        ('en_transito', 'En tránsito'),
        ('entregado', 'Entregado'),
        ('retrasado', 'Retrasado'),
        ('cancelado', 'Cancelado'),
    ]

    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE, related_name='envios_proveedor')
    transportista = models.ForeignKey(Transportista, on_delete=models.CASCADE, related_name='envios_proveedor')
    oc = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE, related_name='envios_proveedor')
    numero_guia = models.CharField(max_length=100, blank=True)
    fecha_envio = models.DateField(blank=True, null=True)
    fecha_entrega_estimada = models.DateField(blank=True, null=True)
    fecha_entrega_real = models.DateField(blank=True, null=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='preparando')
    costo_envio = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    observaciones = models.TextField(blank=True)

    class Meta:
        db_table = 'envios_proveedor'
        verbose_name = 'Envio Proveedor'
        verbose_name_plural = 'Envios Proveedor'

    def __str__(self):
        return str(self.id)

class DevolucionProveedor(models.Model):
    """Devolución a proveedor de lo que calidad rechazó o puso en cuarentena.

    Lo rechazado nunca entró a ``Existencia`` (calidad solo abona lo aprobado), así
    que la SALIDA de inventario que genera es de delta 0: deja el movimiento formal y
    costeado (``inventarios.services.costeo``) sin descontar un stock que no existe.
    ``total`` = cantidad × costo de la OC (lo que se reclama al proveedor).
    """

    class Estatus(models.TextChoices):
        APLICADA = "aplicada", "Aplicada"
        CANCELADA = "cancelada", "Cancelada"

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT)
    folio = models.CharField(max_length=30, unique=True, null=True, blank=True)
    calidad_inspeccion = models.ForeignKey(CalidadInspeccion, on_delete=models.PROTECT, related_name='devoluciones_proveedor')
    recepcion = models.ForeignKey(Recepcion, on_delete=models.PROTECT, related_name='devoluciones_proveedor')
    orden_compra = models.ForeignKey(OrdenCompra, on_delete=models.SET_NULL, null=True, blank=True, related_name='devoluciones_proveedor')
    proveedor = models.ForeignKey(Proveedor, on_delete=models.SET_NULL, null=True, blank=True, related_name='devoluciones')
    almacen = models.ForeignKey(Almacen, on_delete=models.PROTECT, related_name='+')
    estatus = models.CharField(max_length=10, choices=Estatus.choices, default=Estatus.APLICADA, db_index=True)
    motivo = models.TextField()
    fecha = models.DateTimeField(auto_now_add=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    movimiento_inventario = models.ForeignKey(
        'inventarios.MovimientoInventario', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    motivo_cancelacion = models.TextField(blank=True, default='')
    fecha_cancelacion = models.DateTimeField(null=True, blank=True)
    cancelado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'devoluciones_proveedor'
        verbose_name = 'Devolución a proveedor'
        verbose_name_plural = 'Devoluciones a proveedor'
        indexes = [models.Index(fields=['empresa', '-fecha'], name='ix_devprov_emp_fecha')]

    def __str__(self):
        return self.folio or str(self.pk)


class DevolucionProveedorDetalle(models.Model):
    devolucion = models.ForeignKey(DevolucionProveedor, on_delete=models.CASCADE, related_name='detalles')
    calidad_detalle = models.ForeignKey(CalidadInspeccionDetalle, on_delete=models.PROTECT, related_name='devoluciones')
    recepcion_detalle = models.ForeignKey(RecepcionDetalle, on_delete=models.PROTECT, related_name='+')
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    producto_variante = models.ForeignKey(ProductoVariante, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    variante_produccion = models.ForeignKey(VarianteProductoProduccion, on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    cantidad = models.DecimalField(max_digits=14, decimal_places=4)
    costo_unitario = models.DecimalField(max_digits=18, decimal_places=8, default=0)
    importe = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        db_table = 'devoluciones_proveedor_detalle'
        verbose_name = 'Devolución a proveedor detalle'
        verbose_name_plural = 'Devoluciones a proveedor detalle'
