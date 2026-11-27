from django.conf import settings
import hashlib
import secrets

from django.contrib.postgres.indexes import OpClass
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower, Upper
from simple_history.models import HistoricalRecords

from nucleo.indices import GinIndexSoloPostgres
from nucleo.scope import visibles_en_empresa_activa

class Estado(models.TextChoices):
    PENDIENTE = "PENDIENTE", "Pendiente"
    EN_PROCESO = "EN_PROCESO", "En proceso"
    COMPLETADO = "COMPLETADO", "Completado"
    CANCELADO = "CANCELADO", "Cancelado"


class CancelacionMixin(models.Model):
    """Quién, cuándo y por qué se canceló el documento (``wms/services/cancelacion_service``).
    Cancelar no borra: el documento conserva su folio y queda en estado cancelado."""

    motivo_cancelacion = models.CharField(max_length=500, blank=True, default="")
    fecha_cancelacion = models.DateTimeField(null=True, blank=True)
    cancelado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )

    class Meta:
        abstract = True


class LotePicking(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        ASIGNADO = "ASIGNADO", "Asignado"
        EN_PROCESO = "EN_PROCESO", "En proceso"
        EN_SORTING = "EN_SORTING", "En clasificación"  # separando cantidades recolectadas por pedido
        COMPLETADO = "COMPLETADO", "Completado"
        CANCELADO = "CANCELADO", "Cancelado"

    almacen = models.ForeignKey("inventarios.Almacen", on_delete=models.CASCADE, related_name="lotes_picking")
    operador = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="lotes_picking", blank=True, null=True)

    estado = models.CharField(max_length=50, choices=Estado.choices, default=Estado.PENDIENTE)

    total_pedidos = models.IntegerField(default=0)
    total_lineas = models.IntegerField(default=0)

    #estacion_sorting = models.CharField(max_length=50, blank=True, null=True)  # dónde se clasifica lo recolectadoj
    #ruta_optimizada = models.JSONField(blank=True, null=True)  # secuencia de ubicaciones calculada, si aplica
    fecha_inicio = models.DateTimeField(blank=True, null=True)
    fecha_fin = models.DateTimeField(blank=True, null=True)

    usuario = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="lotes_picking_creados")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lotes_picking"
        verbose_name = "Lote Picking"
        verbose_name_plural = "Lotes Picking"

    def __str__(self):
        return f"Lote #{self.id} ({self.estado})"
    
class ZonaAlmacen(models.Model):
    nombre = models.CharField(max_length=50)
    almacen = models.ForeignKey("inventarios.Almacen", on_delete=models.CASCADE, related_name="zonas_almacen")
    tipo_zona = models.CharField(max_length=50)

    class Meta:
        db_table = "zonas_almacen"
        verbose_name = "Zona Almacen"
        verbose_name_plural = "Zonas Almacen"

    def __str__(self):
        return self.nombre

class Oleada(models.Model):
    class Estado(models.TextChoices):
        ABIERTA = "ABIERTA", "Abierta"
        LIBERADA = "LIBERADA", "Liberada"
        EN_PROCESO = "EN_PROCESO", "En proceso"
        CERRADA = "CERRADA", "Cerrada"
        CANCELADA = "CANCELADA", "Cancelada"

    fecha_creacion = models.DateTimeField(auto_now_add=True)
    fecha_liberacion = models.DateTimeField(blank=True, null=True)

    criterio_agrupacion = models.CharField(max_length=50)
    estado = models.CharField(max_length=50, choices=Estado.choices, default=Estado.ABIERTA)
    total_pedidos = models.IntegerField(default=0)
    total_lineas = models.IntegerField(default=0)
    usuario = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="oleadas")

    class Meta:
        db_table = "oleadas"
        verbose_name = "Oleada"
        verbose_name_plural = "Oleadas"

    def __str__(self):
        return f"Oleada #{str(self.id)} ({self.estado})"

class Picking(CancelacionMixin):
    class Prioridad(models.TextChoices):
        BAJA = "BAJA", "Baja"
        MEDIA = "MEDIA", "Media"
        ALTA = "ALTA", "Alta"
    
    class TipoPicking(models.TextChoices):
        ORDER_PICKING = "ORDER_PICKING", "Por pedido"
        BATCH_PICKING = "BATCH_PICKING", "Por lote"
        WAVE_PICKING = "WAVE_PICKING", "Por oleadas"
        ZONE_PICKING = "ZONE_PICKING", "Por zonas"

    class Estado(models.TextChoices):
        PENDIENTE = "Pendiente", "Pendiente"
        ASIGNADO = "Asignado", "Asignado"
        EN_PROCESO = "En proceso", "En proceso"
        PAUSADO = "Pausado", "Pausado"
        COMPLETADO = "Completado", "Completado"
        PARCIAL = "Parcial", "Parcial"
        CANCELADO = "Cancelado", "Cancelado"

    folio = models.CharField(max_length=50)
    empresa = models.ForeignKey("nucleo.Empresa", on_delete=models.CASCADE, related_name="pickings")
    sucursal = models.ForeignKey("nucleo.Sucursal", on_delete=models.CASCADE, related_name="pickings")

    pedido = models.ForeignKey("ventas.Pedido", on_delete=models.CASCADE, related_name="pickings")
    operador = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="pickings_operadores")
    almacen = models.ForeignKey("inventarios.Almacen", on_delete=models.CASCADE, related_name="pickings")
    almacen_destino = models.ForeignKey("inventarios.Almacen", on_delete=models.CASCADE, related_name="pickings_destino", blank=True, null=True,)

    class ProgramacionDestino(models.TextChoices):
        BORDADO = "BORDADO", "Bordado"
        REFLEJANTE = "REFLEJANTE", "Reflejante"
        CORTE_MANGA = "CORTE_MANGA", "Corte de manga"
        EMBARQUE = "EMBARQUE", "Embarque"
        APARTADO = "APARTADO", "Apartado"

    # A qué programación del pedido (``programacion_conf.programaciones[].destino``)
    # abona este surtido. Nulo en pickings anteriores sin destino inferible.
    programacion_destino = models.CharField(
        max_length=15, choices=ProgramacionDestino.choices, null=True, blank=True
    )

    transferencia = models.ForeignKey(
        "wms.Transferencia",
        on_delete=models.SET_NULL,
        related_name="pickings",
        blank=True,
        null=True,
    )

    oleada = models.ForeignKey(Oleada, on_delete=models.CASCADE, related_name="pickings", blank=True, null=True)
    zona_almacen = models.ForeignKey(ZonaAlmacen, on_delete=models.CASCADE, related_name="pickings", blank=True, null=True)
    lote = models.ForeignKey(LotePicking, on_delete=models.CASCADE, related_name="pickings", blank=True, null=True)

    prioridad = models.CharField(max_length=50, choices=Prioridad.choices, default=Prioridad.MEDIA)
    tipo = models.CharField(max_length=50, choices=TipoPicking.choices, default=TipoPicking.ORDER_PICKING)
    estado = models.CharField(max_length=50, choices=Estado.choices, default=Estado.PENDIENTE)

    fecha_inicio = models.DateTimeField(blank=True, null=True)
    fecha_fin = models.DateTimeField(blank=True, null=True)
    fecha_limite = models.DateTimeField(blank=True, null=True)

    total_lineas = models.IntegerField(default=0)
    total_lineas_completas = models.IntegerField(default=0)

    observaciones = models.TextField(blank=True, null=True)

    usuario = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="pickings_usuarios")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "picking"
        verbose_name = "Picking"
        verbose_name_plural = "Pickings"
        indexes = [
            # ``?search=`` del listado (EC-921) busca el folio por subcadena
            # (``folio__icontains`` → ``UPPER("folio"::text) LIKE UPPER('%...%')``):
            # un GIN trigram sobre ``UPPER(folio)`` sí sirve a eso. Mismo patrón que
            # los folios de producción; sólo existe en PostgreSQL (``nucleo.indices``).
            GinIndexSoloPostgres(
                OpClass(Upper("folio"), name="gin_trgm_ops"),
                name="picking_folio_upper_trgm",
            ),
        ]

    def __str__(self):
        return self.folio

    @property
    def almacen_actual(self):
        """Dónde están hoy las prendas: en el destino si la TR del picking ya las movió;
        si no (picking sin TR, TR no completada o ya trasladado), en ``almacen`` (#701)."""
        if (
            self.transferencia_id and self.almacen_destino_id
            and self.transferencia.status == Transferencia.TransferenciaStatus.COMPLETADA
        ):
            return self.almacen_destino
        return self.almacen

class PickingDetalle(models.Model):
    class EstadoLinea(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        SURTIDA = "SURTIDA", "Surtida"
        PARCIAL = "PARCIAL", "Parcial"
        FALTANTE = "FALTANTE", "Faltante"
        CANCELADA = "CANCELADA", "Cancelada"

    picking = models.ForeignKey(Picking, on_delete=models.CASCADE, related_name="picking_detalle")
    pedido_detalle = models.ForeignKey("ventas.PedidoDetalle", on_delete=models.CASCADE, related_name="picking_detalle")
    pedido_detalle_talla = models.ForeignKey(
        "ventas.PedidoDetalleTalla",
        on_delete=models.CASCADE,
        related_name="picking_detalle",
        null=True,
        blank=True,
    )

    producto = models.ForeignKey("catalogo.Producto", on_delete=models.CASCADE, related_name="picking_detalle", blank=True, null=True)
    producto_variante = models.ForeignKey("catalogo.ProductoVariante", on_delete=models.CASCADE, related_name="picking_detalle", blank=True, null=True)
    # Línea de muestra: su SKU de muestra (sin producto ni variante de catálogo).
    variante_produccion = models.ForeignKey(
        "catalogo.VarianteProductoProduccion", on_delete=models.PROTECT, related_name="picking_detalle", blank=True, null=True
    )
    ubicacion = models.ForeignKey("inventarios.Ubicacion", on_delete=models.CASCADE, related_name="picking_detalle", blank=True, null=True)
    lote = models.ForeignKey("inventarios.Lote", on_delete=models.CASCADE, related_name="picking_detalle", blank=True, null=True)

    cantidad_solicitada = models.DecimalField(max_digits=18, decimal_places=4)
    cantidad_asignada = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    cantidad_surtida = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    estado = models.CharField(max_length=50, choices=EstadoLinea.choices, default=EstadoLinea.PENDIENTE)
    operador = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="picking_detalle", blank=True, null=True)

    fecha_surtido = models.DateTimeField(blank=True, null=True)
    
    diferencia = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    motivo_diferencia = models.CharField(max_length=100, blank=True, null=True)

    observaciones = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "picking_detalle"
        verbose_name = "Picking Detalle"
        verbose_name_plural = "Picking Detalles"

    def __str__(self):
        return str(self.id)

class Packing(CancelacionMixin):
    folio = models.CharField(max_length=50)
    empresa = models.ForeignKey("nucleo.Empresa", on_delete=models.CASCADE, related_name="packings")
    sucursal = models.ForeignKey("nucleo.Sucursal", on_delete=models.CASCADE, related_name="packings")

    pedido = models.ForeignKey("ventas.Pedido", on_delete=models.CASCADE, related_name="packings")
    picking = models.ForeignKey(Picking, on_delete=models.CASCADE, related_name="packings")
    operador = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="packings_operadores")
    estado = models.CharField(max_length=50, choices=Estado.choices, default=Estado.PENDIENTE)

    numero_cajas = models.PositiveIntegerField(default=0)
    peso_total = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    volumen_total = models.DecimalField(max_digits=18, decimal_places=3, default=0)

    observaciones = models.TextField(blank=True, null=True)
    fecha_inicio = models.DateTimeField(blank=True, null=True)
    fecha_fin = models.DateTimeField(blank=True, null=True)

    usuario = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="packings_usuarios")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "packing"
        verbose_name = "Packing"
        verbose_name_plural = "Packings"

    def __str__(self):
        return self.folio

class PackingCaja(models.Model):
    packing = models.ForeignKey(Packing, on_delete=models.CASCADE, related_name="packing_cajas")
    numero = models.PositiveIntegerField()

    peso = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    largo = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    ancho = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    alto = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    observaciones = models.TextField(blank=True, null=True)

class PackingDetalle(models.Model):
    packing = models.ForeignKey(Packing, on_delete=models.CASCADE, related_name="packing_detalle")
    caja = models.ForeignKey(PackingCaja, on_delete=models.CASCADE, related_name="packing_detalle", blank=True, null=True)
    picking_detalle = models.ForeignKey(PickingDetalle, on_delete=models.CASCADE, related_name="packing_detalle")

    cantidad_empacada = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    estado = models.CharField(max_length=50, choices=Estado.choices, default=Estado.PENDIENTE)

    observaciones = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "packing_detalle"
        verbose_name = "Packing Detalle"
        verbose_name_plural = "Packing Detalles"

    def __str__(self):
        return str(self.id)

class Despacho(CancelacionMixin):
    packing = models.ForeignKey(Packing, on_delete=models.CASCADE, related_name="despachos")
    envio = models.ForeignKey("logistica.Envio", on_delete=models.CASCADE, related_name="despachos", null=True, blank=True)
    guia = models.CharField(max_length=100, blank=True, default="")

    class Estado(models.TextChoices):
        ACTIVO = "ACTIVO", "Activo"
        CANCELADO = "CANCELADO", "Cancelado"

    # Un despacho cancelado deja de contar como despachado y su salida se revierte
    # con una ENTRADA ligada al mismo despacho.
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.ACTIVO)

    class TipoEnvio(models.TextChoices):
        PAQUETERIA = "PAQUETERIA", "Paquetería"
        LOCAL = "LOCAL", "Local"

    class Paqueteria(models.TextChoices):
        ESTAFETA = "ESTAFETA", "Estafeta"
        FEDEX = "FEDEX", "FedEx"
        DHL = "DHL", "DHL"
        PAQUETEXPRESS = "PAQUETEXPRESS", "Paquetexpress"
        UPS = "UPS", "UPS"
        NOVENTA_Y_NUEVE_MINUTOS = "99MINUTOS", "99 Minutos"
        OTRA = "OTRA", "Otra"

    # Obligatorio en el alta (serializer); nulo en despachos anteriores.
    tipo_envio = models.CharField(max_length=12, choices=TipoEnvio.choices, null=True, blank=True)
    paqueteria = models.CharField(max_length=15, choices=Paqueteria.choices, blank=True, default="")
    chofer = models.ForeignKey(
        "hr.Empleado", on_delete=models.PROTECT, related_name="despachos", null=True, blank=True
    )
    # #1361: señal de entrega (``POST despachos/{id}/entregar/``). Entregado ya no se cancela.
    fecha_entrega = models.DateTimeField(null=True, blank=True)
    entregado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="+", null=True, blank=True,
    )
    recibio = models.CharField(max_length=150, blank=True, default="")
    # Alta del despacho; en los anteriores se tomó de su movimiento de salida (si lo tiene).
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="despachos_registrados",
        null=True, blank=True,
    )

    class Meta:
        db_table = "despachos"
        verbose_name = "Despacho"
        verbose_name_plural = "Despachos"

    def __str__(self):
        return str(self.id)

    # El despacho no guarda empresa ni sucursal: son las de su packing. La bitácora de
    # documentos (``auditoria.bitacora``) las lee por estos nombres.
    @property
    def empresa_id(self):
        return self.packing.empresa_id if self.packing_id else None

    @property
    def sucursal_id(self):
        return self.packing.sucursal_id if self.packing_id else None

class DespachoDetalle(models.Model):
    despacho = models.ForeignKey(
        Despacho, on_delete=models.CASCADE, related_name="despacho_detalle"
    )
    packing_detalle = models.ForeignKey(
        PackingDetalle, on_delete=models.CASCADE, related_name="despacho_detalle"
    )

    class Meta:
        db_table = "despacho_detalle"
        verbose_name = "Despacho Detalle"
        verbose_name_plural = "Despacho Detalles"

    def __str__(self):
        return str(self.id)

class ConteoCiclico(models.Model):
    almacen = models.ForeignKey(
        "inventarios.Almacen", on_delete=models.CASCADE, related_name="conteo_ciclico"
    )

    class Meta:
        db_table = "conteos_ciclico"
        verbose_name = "Conteo Ciclico"
        verbose_name_plural = "Conteos Cíclicos"

    def __str__(self):
        return str(self.id)

class ConteoCiclicoDetalle(models.Model):
    conteo_ciclico = models.ForeignKey(
        ConteoCiclico, on_delete=models.CASCADE, related_name="conteo_ciclico_detalle"
    )
    existencia = models.ForeignKey(
        "inventarios.Existencia",
        on_delete=models.CASCADE,
        related_name="conteo_ciclico_detalle",
    )

    class Meta:
        db_table = "conteo_ciclico_detalle"
        verbose_name = "Conteo Ciclico Detalle"
        verbose_name_plural = "Conteos Cíclicos Detalles"

    def __str__(self):
        return str(self.id)

class PickingOrdenTrabajo(models.Model):
    class TipoOrden(models.TextChoices):
        BORDADO = "BORDADO", "Bordado"
        REFLEJANTE = "REFLEJANTE", "Reflejante"
        CORTE_MANGA = "CORTE_MANGA", "Corte de manga"

    picking = models.ForeignKey(Picking, on_delete=models.CASCADE, related_name="ordenes_trabajo",)
    tipo_orden = models.CharField(max_length=30, choices=TipoOrden.choices)
    orden_bordado = models.ForeignKey("produccion.OrdenesBordado", on_delete=models.SET_NULL, related_name="picking_ordenes_trabajo", blank=True, null=True,)
    orden_reflejante = models.ForeignKey("produccion.OrdenesReflejante", on_delete=models.SET_NULL, related_name="picking_ordenes_trabajo", blank=True, null=True,)
    orden_corte_manga = models.ForeignKey("produccion.OrdenesCorteManga", on_delete=models.SET_NULL, related_name="picking_ordenes_trabajo", blank=True, null=True,)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "picking_orden_trabajo"
        verbose_name = "Picking Orden Trabajo"
        verbose_name_plural = "Picking Ordenes Trabajo"

    def __str__(self):
        return f"Picking {self.picking_id} - {self.tipo_orden}"


class Transferencia(CancelacionMixin):
    class TransferenciaStatus(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        EN_PROCESO = "EN_PROCESO", "En proceso"
        COMPLETADA = "COMPLETADA", "Completada"
        CANCELADA = "CANCELADA", "Cancelada"
        FALLIDA = "FALLIDA", "Fallida"

    empresa = models.ForeignKey(
        "nucleo.Empresa", on_delete=models.CASCADE, related_name="transferencias"
    )
    sucursal = models.ForeignKey(
        "nucleo.Sucursal", on_delete=models.CASCADE, related_name="transferencias"
    )

    almacen_origen = models.ForeignKey(
        "inventarios.Almacen",
        on_delete=models.CASCADE,
        related_name="transferencias_origen",
    )
    almacen_destino = models.ForeignKey(
        "inventarios.Almacen",
        on_delete=models.CASCADE,
        related_name="transferencias_destino",
    )

    folio = models.CharField(max_length=50)
    # Picking que originó la TR: su alta y cada traslado (EC-916). ``Picking.transferencia``
    # solo guarda la última; esta FK conserva todas. La fija el servidor (no la API);
    # null en las TR manuales y en las históricas sin evidencia clara (backfill 0021).
    picking = models.ForeignKey(
        Picking,
        on_delete=models.SET_NULL,
        related_name="transferencias",
        blank=True,
        null=True,
    )

    # TR que esta deshace (``revertir``): mismas piezas, almacenes invertidos.
    reversa_de = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="reversa", null=True, blank=True
    )

    usuario = models.ForeignKey(
        "usuarios.Usuario", on_delete=models.CASCADE, related_name="transferencias"
    )
    observaciones = models.TextField(blank=True, null=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        default=TransferenciaStatus.COMPLETADA,
        choices=TransferenciaStatus.choices,
    )

    history = HistoricalRecords()

    class Meta:
        db_table = "transferencias"
        verbose_name = "Transferencia"
        verbose_name_plural = "Transferencias"

    def __str__(self):
        return str(self.id)

class TransferenciaDetalle(models.Model):
    transferencia = models.ForeignKey(
        Transferencia, on_delete=models.CASCADE, related_name="transferencia_detalle"
    )
    producto = models.ForeignKey(
        "catalogo.Producto",
        on_delete=models.CASCADE,
        related_name="transferencia_detalle",
        blank=True,
        null=True,
    )
    producto_variante = models.ForeignKey(
        "catalogo.ProductoVariante",
        on_delete=models.CASCADE,
        related_name="transferencia_detalle_variante",
        blank=True,
        null=True,
    )
    # SKU de muestra (``inventarios.Existencia.variante_produccion``).
    variante_produccion = models.ForeignKey(
        "catalogo.VarianteProductoProduccion",
        on_delete=models.PROTECT,
        related_name="transferencia_detalle",
        blank=True,
        null=True,
    )
    cantidad = models.DecimalField(max_digits=18, decimal_places=4)
    ubicacion_origen = models.ForeignKey(
        "inventarios.Ubicacion",
        on_delete=models.CASCADE,
        related_name="transferencia_detalle_origen",
        blank=True,
        null=True,
    )
    ubicacion_destino = models.ForeignKey(
        "inventarios.Ubicacion",
        on_delete=models.CASCADE,
        related_name="transferencia_detalle_destino",
        blank=True,
        null=True,
    )
    lote = models.ForeignKey(
        "inventarios.Lote",
        on_delete=models.CASCADE,
        related_name="transferencia_detalle",
        blank=True,
        null=True,
    )
    serie = models.ForeignKey(
        "inventarios.Serie",
        on_delete=models.CASCADE,
        related_name="transferencia_detalle",
        blank=True,
        null=True,
    )

    class Meta:
        db_table = "transferencia_detalle"
        verbose_name = "Transferencia Detalle"
        verbose_name_plural = "Transferencias Detalles"

    def __str__(self):
        return str(self.id)


class EtiquetaRFIDImpresion(models.Model):
    class Estatus(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        EXITO = "EXITO", "Éxito"
        FALLIDO = "FALLIDO", "Fallido"

    empresa = models.ForeignKey(
        "nucleo.Empresa",
        on_delete=models.CASCADE,
        related_name="etiquetas_rfid_impresiones",
    )
    sucursal = models.ForeignKey(
        "nucleo.Sucursal",
        on_delete=models.CASCADE,
        related_name="etiquetas_rfid_impresiones",
        null=True,
        blank=True,
    )
    usuario = models.ForeignKey(
        "usuarios.Usuario",
        on_delete=models.CASCADE,
        related_name="etiquetas_rfid_impresiones",
        null=True,
        blank=True,
    )
    producto = models.ForeignKey(
        "catalogo.Producto",
        on_delete=models.CASCADE,
        related_name="etiquetas_rfid_impresiones",
        null=True,
        blank=True,
    )
    producto_variante = models.ForeignKey(
        "catalogo.ProductoVariante",
        on_delete=models.CASCADE,
        related_name="etiquetas_rfid_impresiones",
        null=True,
        blank=True,
    )
    cantidad = models.PositiveIntegerField(default=1)
    rfid_mode = models.BooleanField(default=True)
    printer_name = models.CharField(max_length=128, null=True, blank=True)
    printer_address = models.CharField(max_length=128, null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Estatus.choices, default=Estatus.PENDIENTE
    )
    zpl_enviado = models.TextField(null=True, blank=True)
    observaciones = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "etiquetas_rfid_impresiones"
        verbose_name = "Impresión Etiqueta RFID"
        verbose_name_plural = "Impresiones Etiquetas RFID"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"LAB-{self.pk:06d}"

    @property
    def folio(self):
        return f"LAB-{self.pk:06d}"


class EtiquetaRFIDDetalle(models.Model):
    class Estado(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        IMPRESO = "IMPRESO", "Impreso"
        LEIDO = "LEIDO", "Leído"
        CANCELADO = "CANCELADO", "Cancelado"

    impresion = models.ForeignKey(
        EtiquetaRFIDImpresion,
        on_delete=models.CASCADE,
        related_name="etiquetas",
    )
    epc = models.CharField(max_length=64, unique=True, db_index=True)
    barcode_value = models.CharField(max_length=128)
    serial = models.CharField(max_length=64, null=True, blank=True)
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.PENDIENTE
    )
    # Primera vez que un lector la leyó (pasa a ``LEIDO``).
    leida_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "etiquetas_rfid_detalle"
        verbose_name = "Detalle Etiqueta RFID"
        verbose_name_plural = "Detalles Etiquetas RFID"
        ordering = ["impresion_id", "id"]

    def __str__(self):
        return self.epc


def _generar_token_lector():
    # Lo importan las migraciones viejas (default del antiguo ``token``).
    return secrets.token_urlsafe(32)


def hash_token_lector(token):
    """SHA-256 del token del lector. El token es aleatorio de 256 bits, así que
    no hace falta un hash lento (no se puede adivinar por diccionario)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LectorRFID(models.Model):
    empresa = models.ForeignKey(
        "nucleo.Empresa",
        on_delete=models.CASCADE,
        related_name="lectores_rfid",
    )
    # Obligatoria en formularios; nullable en BD solo por lectores dados de alta
    # antes de exigirla. Un lector sin sucursal solo lo ven los admins.
    sucursal = models.ForeignKey(
        "nucleo.Sucursal",
        on_delete=models.PROTECT,
        related_name="lectores_rfid",
        null=True,
    )
    nombre = models.CharField(max_length=100)
    # Dónde está físicamente (andén, puerta...). Texto libre, opcional.
    ubicacion = models.CharField(max_length=150, blank=True, default="")
    # Solo el hash: el token en claro se muestra una vez, al crear el lector o al
    # regenerarlo (``asignar_token``). ``token_sufijo`` sirve para enmascararlo.
    token_hash = models.CharField(max_length=64, unique=True)
    token_sufijo = models.CharField(max_length=4, blank=True, default="")
    # Rate limit por token (#receive-scan): peticiones en la ventana de 1 min
    # que empezó en ``rl_ventana``. Lo lleva el mismo UPDATE del heartbeat.
    rl_ventana = models.DateTimeField(null=True, blank=True)
    rl_conteo = models.PositiveIntegerField(default=0)
    # ``False`` = eliminado desde el ERP: su token deja de valer y sus lecturas
    # se conservan (soft delete).
    activo = models.BooleanField(default=True)
    ultima_lectura = models.DateTimeField(null=True, blank=True)
    # Último POST autenticado del FX, traiga tags o no (heartbeat incluido): con
    # ``ultima_lectura`` distingue "lector apagado" de "encendido sin tags".
    ultima_conexion = models.DateTimeField(null=True, blank=True)
    # "En uso" (informativo, no bloquea): último latido de la pantalla que está
    # escaneando con este lector. Pasados ``SEGUNDOS_EN_USO`` sin latido, libre.
    en_uso_usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )
    en_uso_pantalla = models.CharField(max_length=50, blank=True, default="")
    en_uso_documento = models.CharField(max_length=100, blank=True, default="")
    en_uso_desde = models.DateTimeField(null=True, blank=True)
    en_uso_latido = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "lectores_rfid"
        verbose_name = "Lector RFID"
        verbose_name_plural = "Lectores RFID"
        ordering = ["empresa_id", "nombre"]
        constraints = [
            # Nombre único por empresa entre los lectores vigentes; uno eliminado
            # libera su nombre.
            models.UniqueConstraint(
                Lower("nombre"), "empresa",
                condition=models.Q(activo=True),
                name="uq_lector_rfid_empresa_nombre_activo",
            ),
        ]

    def __str__(self):
        return f"{self.nombre} ({self.empresa_id})"

    def asignar_token(self):
        """Genera un token nuevo, guarda su hash y lo deja en ``self.token`` (solo
        en memoria) para mostrarlo esa única vez. No guarda: lo hace quien llama."""
        token = _generar_token_lector()
        self.token = token
        self.token_hash = hash_token_lector(token)
        self.token_sufijo = token[-4:]
        return token

    def save(self, *args, **kwargs):
        if not self.token_hash:
            self.asignar_token()
        super().save(*args, **kwargs)

    def clean(self):
        if self.sucursal_id and self.empresa_id and self.sucursal.empresa_id != self.empresa_id:
            raise ValidationError({"sucursal": "La sucursal no pertenece a la empresa del lector."})


class RfidScanQuerySet(models.QuerySet):
    def visibles_para(self, user):
        """Lecturas que ``user`` puede ver o purgar: SIEMPRE su empresa activa.

        Mismo predicado de empresa que picking, packing y TR (EC-882,
        ``nucleo.scope.empresa_ids_lectura``), superusuario incluido (EC-884):
        sin empresa activa no se ve nada. Dentro de la empresa, superusuario e
        ``is_admin_empresa`` ven todas las sucursales; el resto, sólo las de
        ``user.sucursales_permitidas()``. ``RfidScan`` no tiene ``sucursal``: usa
        ``visibles_en_empresa_activa`` con la sucursal del lector o, sin lector, de
        la etiqueta impresa.

        Las lecturas sin empresa (tags ajenos de ``/api/receive-scan/`` sin
        token) no las ve nadie por la API; se borran desde el Django admin.
        """
        # Empresa y staff: la regla compartida (#1156); aquí sólo cambia la sucursal.
        # Sin lector (POST /api/receive-scan/, sin token): la de la etiqueta impresa.
        def por_sucursal(u):
            sucursales = u.sucursales_permitidas()
            return (models.Q(lector__sucursal_id__in=sucursales)
                    | models.Q(lector__isnull=True, etiqueta__impresion__sucursal_id__in=sucursales))

        return visibles_en_empresa_activa(self, user, filtro_sucursal=por_sucursal)


class RfidScan(models.Model):
    # Nullable solo por las lecturas previas al token por lector.
    empresa = models.ForeignKey(
        "nucleo.Empresa",
        on_delete=models.CASCADE,
        related_name="rfid_scans",
        null=True,
        blank=True,
    )
    lector = models.ForeignKey(
        LectorRFID,
        on_delete=models.SET_NULL,
        related_name="scans",
        null=True,
        blank=True,
    )
    epc = models.CharField(max_length=255, db_index=True)
    # Etiqueta impresa por el ERP con este EPC. Se liga al recibir la lectura o,
    # si la lectura llegó antes que el registro, al registrar la impresión.
    # Nula = tag ajeno a nuestras impresiones (de fábrica, de otro proveedor...).
    etiqueta = models.ForeignKey(
        EtiquetaRFIDDetalle,
        on_delete=models.SET_NULL,
        related_name="lecturas",
        null=True,
        blank=True,
    )
    reader_ip = models.GenericIPAddressField(null=True, blank=True)
    antenna = models.IntegerField(null=True, blank=True)
    rssi = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = RfidScanQuerySet.as_manager()

    class Meta:
        db_table = "rfid_scans"
        verbose_name = "Lectura RFID"
        verbose_name_plural = "Lecturas RFID"
        ordering = ["-created_at", "-id"]
        indexes = [
            # Polling de ``scans/`` (lo más nuevo primero), por empresa y por lector.
            models.Index(fields=["empresa", "-created_at", "-id"], name="rfid_scan_emp_fecha_idx"),
            models.Index(fields=["lector", "-created_at", "-id"], name="rfid_scan_lector_fecha_idx"),
            # Cursor ``despues_de_id`` (EC-984): ``id > N`` en orden ascendente,
            # sin recorrer las lecturas de otras empresas u otros lectores.
            models.Index(fields=["empresa", "id"], name="rfid_scan_emp_id_idx"),
            models.Index(fields=["lector", "id"], name="rfid_scan_lector_id_idx"),
            # Retención: borra por antigüedad.
            models.Index(fields=["created_at"], name="rfid_scan_fecha_idx"),
        ]

    def __str__(self):
        return f"{self.epc} @ {self.created_at.isoformat()}"


class RfidLecturaUltima(models.Model):
    """Última lectura guardada de cada EPC por lector (una fila por par).

    Mantiene ``scans/?unicos=1`` acotado al número de tags distintos, no al de
    lecturas, y deja deduplicar la ingesta (``DEDUP_SEGUNDOS``) con una sola
    consulta por índice único. Se borra en cascada con su ``RfidScan``.
    """

    empresa = models.ForeignKey(
        "nucleo.Empresa", on_delete=models.CASCADE, related_name="+",
    )
    lector = models.ForeignKey(
        LectorRFID, on_delete=models.CASCADE, related_name="ultimas_lecturas",
    )
    epc = models.CharField(max_length=255)
    scan = models.OneToOneField(
        RfidScan, on_delete=models.CASCADE, related_name="es_ultima",
    )
    visto = models.DateTimeField()

    class Meta:
        db_table = "rfid_lecturas_ultimas"
        verbose_name = "Última lectura RFID"
        verbose_name_plural = "Últimas lecturas RFID"
        constraints = [
            models.UniqueConstraint(fields=["lector", "epc"], name="uq_rfid_ultima_lector_epc"),
        ]
        indexes = [
            models.Index(fields=["empresa", "epc", "-visto"], name="rfid_ultima_emp_epc_idx"),
        ]

    def __str__(self):
        return f"{self.epc} @ {self.lector_id}"
