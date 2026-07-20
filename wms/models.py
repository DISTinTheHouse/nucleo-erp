from django.db import models
from simple_history.models import HistoricalRecords

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

    #estacion_sorting = models.CharField(max_length=50, blank=True, null=True)  # dónde se clasifica lo recolectado
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

class Picking(models.Model):
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

    pedido = models.ForeignKey("ventas.Pedido", on_delete=models.CASCADE, related_name="pickings")
    operador = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="pickings_operadores")
    almacen = models.ForeignKey("inventarios.Almacen", on_delete=models.CASCADE, related_name="pickings")

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

    def __str__(self):
        return str(self.id)

class PickingDetalle(models.Model):
    class EstadoLinea(models.TextChoices):
        PENDIENTE = "PENDIENTE", "Pendiente"
        SURTIDA = "SURTIDA", "Surtida"
        PARCIAL = "PARCIAL", "Parcial"
        FALTANTE = "FALTANTE", "Faltante"
        CANCELADA = "CANCELADA", "Cancelada"

    picking = models.ForeignKey(Picking, on_delete=models.CASCADE, related_name="picking_detalle")
    pedido_detalle = models.ForeignKey("ventas.PedidoDetalle", on_delete=models.CASCADE, related_name="picking_detalle")

    producto = models.ForeignKey("catalogo.Producto", on_delete=models.CASCADE, related_name="picking_detalle", blank=True, null=True)
    producto_variante = models.ForeignKey("catalogo.ProductoVariante", on_delete=models.CASCADE, related_name="picking_detalle", blank=True, null=True)
    ubicacion = models.ForeignKey("inventarios.Ubicacion", on_delete=models.CASCADE, related_name="picking_detalle")
    lote = models.ForeignKey("inventarios.Lote", on_delete=models.CASCADE, related_name="picking_detalle", blank=True, null=True)

    cantidad_solicitada = models.DecimalField(max_digits=18, decimal_places=4)
    cantidad_asignada = models.DecimalField(max_digits=18, decimal_places=4)
    cantidad_surtida = models.DecimalField(max_digits=18, decimal_places=4)

    unidad_medida = models.ForeignKey("nucleo.UnidadMedida", on_delete=models.CASCADE, related_name="picking_detalle")
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

class Packing(models.Model):
    picking = models.ForeignKey(
        Picking, on_delete=models.CASCADE, related_name="packings"
    )

    class Meta:
        db_table = "packing"
        verbose_name = "Packing"
        verbose_name_plural = "Packings"

    def __str__(self):
        return str(self.id)

class PackingDetalle(models.Model):
    packing = models.ForeignKey(
        Packing, on_delete=models.CASCADE, related_name="packing_detalle"
    )
    picking_detalle = models.ForeignKey(
        PickingDetalle, on_delete=models.CASCADE, related_name="packing_detalle"
    )

    class Meta:
        db_table = "packing_detalle"
        verbose_name = "Packing Detalle"
        verbose_name_plural = "Packing Detalles"

    def __str__(self):
        return str(self.id)

class Despacho(models.Model):
    packing = models.ForeignKey(
        Packing, on_delete=models.CASCADE, related_name="despachos"
    )
    envio = models.ForeignKey(
        "logistica.Envio", on_delete=models.CASCADE, related_name="despachos"
    )

    class Meta:
        db_table = "despachos"
        verbose_name = "Despacho"
        verbose_name_plural = "Despachos"

    def __str__(self):
        return str(self.id)

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

class Transferencia(models.Model):
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
