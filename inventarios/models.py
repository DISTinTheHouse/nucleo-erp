from django.db import models
from nucleo.models import Empresa, Sucursal
from catalogo.models import Producto, ProductoVariante
from ventas.models import Pedido, Entrega, Devolucion
from nucleo.models import StatusLifecycleModel
from simple_history.models import HistoricalRecords

class TipoAlmacen(models.TextChoices):
    MATERIA_PRIMA = "MP", "Materia Prima"
    PRODUCTO_TERMINADO = "PT", "Producto Terminado"
    EN_PROCESO = "PROCESO", "En Proceso"
    CUARENTENA = "CUARENTENA", "Cuarentena"
    DEVOLUCION = "DEVOLUCION", "Devolucion"
    TRANSITO = "TRANSITO", "Transito"

class TipoUbicacion(models.TextChoices):
    INVENTARIO = "INVENTARIO", "Inventario"
    RECIBO = "RECIBO", "Recibo"
    PICKING = "PICKING", "Picking"
    RESERVA = "RESERVA", "Reserva"
    STAGING = "STAGING", "Staging"
    EMBARQUE = "EMBARQUE", "Embarque"
    CUARENTENA = "CUARENTENA", "Cuarentena"
    DEVOLUCION = "DEVOLUCION", "Devolucion"

class EstatusUbicacion(models.TextChoices):
    ACTIVO = "ACTIVO", "Activo"
    INACTIVO = "INACTIVO", "Inactivo"
    BLOQUEADO = "BLOQUEADO", "Bloqueado"

class TipoMovimiento(models.TextChoices):
    ENTRADA = "ENTRADA", "Entrada"
    SALIDA = "SALIDA", "Salida"
    AJUSTE = "AJUSTE", "Ajuste"
    TRANSFERENCIA = "TRANSFERENCIA", "Transferencia"

class Almacen(models.Model):
    id_almacen = models.BigAutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="almacenes", null=True, blank=True)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="almacenes", null=True, blank=True)
    codigo = models.CharField(max_length=50, default='')
    nombre = models.CharField(max_length=255, default='')
    estatus = models.CharField(max_length=20, default='ACTIVO', choices=[('ACTIVO', 'Activo'), ('INACTIVO', 'Inactivo')])
    tipo_almacen = models.CharField(max_length=20, choices=TipoAlmacen.choices, default=TipoAlmacen.MATERIA_PRIMA)
    
    requiere_ubicacion = models.BooleanField(default=False)
    permite_entrada = models.BooleanField(default=False)
    permite_salida = models.BooleanField(default=False)
    permite_transferencia = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "almacenes"
        verbose_name = "Almacen"
        verbose_name_plural = "Almacenes"
        constraints = [
            models.UniqueConstraint(fields=['sucursal', 'codigo'], name='uq_almacen_sucursal_codigo')
        ]
        indexes = [
            models.Index(fields=['empresa', 'estatus'])
        ]

    def __str__(self):
        return f"{self.nombre}"

class Ubicacion(models.Model):
    id_ubicacion = models.BigAutoField(primary_key=True)
    almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE, related_name="ubicaciones", null=True, blank=True)
    tipo_ubicacion = models.CharField(max_length=20, choices=TipoUbicacion.choices, default=TipoUbicacion.RESERVA)
    estatus = models.CharField(max_length=20, choices=EstatusUbicacion.choices, default=EstatusUbicacion.ACTIVO)
    
    pasillo = models.CharField(max_length=50, default='')
    rack = models.CharField(max_length=50, default='')
    nivel = models.CharField(max_length=50, default='')
    posicion = models.CharField(max_length=50, default='')
    
    orden_recorrido = models.PositiveIntegerField(default=0)
    
    bloqueada_entrada = models.BooleanField(default=False)
    bloqueada_salida = models.BooleanField(default=False)
    permite_mezcla_productos = models.BooleanField(default=False)
    permite_mezcla_lotes = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "ubicaciones"
        verbose_name = "Ubicacion"
        verbose_name_plural = "Ubicaciones"
        constraints = [
            models.UniqueConstraint(fields=['almacen', 'pasillo', 'rack', 'nivel', 'posicion'], name='uq_ubicacion_coords')
        ]

    def __str__(self):
        return f"{self.almacen.nombre} - {self.pasillo}-{self.rack}-{self.nivel}-{self.posicion}"

class Lote(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="lotes", null=True, blank=True)

    def __str__(self):
        return str(self.id)

class Serie(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="series", null=True, blank=True)

    def __str__(self):
        return str(self.id)

class AjusteInventario(StatusLifecycleModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="ajustes_inventario")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="ajustes_inventario")
    almacen = models.ForeignKey(Almacen, on_delete=models.PROTECT, related_name="ajustes_inventario")

    fecha_ajuste = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    motivo = models.CharField(max_length=100)
    observaciones = models.TextField(max_length=150, null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        db_table = "ajustes_inventario"
        verbose_name = "Ajuste Inventario"
        verbose_name_plural = "Ajustes Inventario"

    def __str__(self):
        return str(self.id)

class AjusteDetalle(models.Model):
    ajuste = models.ForeignKey(AjusteInventario, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.CASCADE)
    lote = models.ForeignKey(Lote, on_delete=models.CASCADE)
    serie = models.ForeignKey(Serie, on_delete=models.CASCADE)

    cantidad_sistema = models.DecimalField(max_digits=18, decimal_places=4)
    cantidad_fisica = models.DecimalField(max_digits=18, decimal_places=4)
    diferencia = models.DecimalField(max_digits=18, decimal_places=4)

    class Meta:
        db_table = "ajuste_detalle"
        verbose_name = "Ajuste detalle"
        verbose_name_plural = "Ajustes Detalle"
    
    def __str__(self):
        return str(self.id)

class Existencia(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, null=True, blank=True)
    producto_variante = models.ForeignKey(ProductoVariante, on_delete=models.PROTECT, related_name="existencia", null=True, blank=True)
    almacen = models.ForeignKey(Almacen, on_delete=models.PROTECT, related_name="existencia")
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.PROTECT, related_name="existencia", null=True, blank=True)
    stock = models.IntegerField(default=0)
    cantidad = models.DecimalField(max_digits=18, decimal_places=4)
    fecha_actualizacion = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        db_table = "existencias"
        verbose_name = "Existencia"
        verbose_name_plural = "Existencia"

    def __str__(self):
        producto = self.producto or getattr(self.producto_variante, "producto", None)
        nombre = getattr(producto, "nombre", "Sin producto")
        return f"{nombre} - {self.almacen.nombre}"

class MovimientoInventario(StatusLifecycleModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="movimientos_inventario")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="movimientos_inventario")
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="movimientos_inventario", null=True, blank=True)
    entrega = models.ForeignKey(Entrega, on_delete=models.CASCADE, related_name="movimientos_inventario", null=True, blank=True)
    devolucion = models.ForeignKey(Devolucion, on_delete=models.CASCADE, related_name="movimientos_inventario", null=True, blank=True)
    ajuste_inventario = models.ForeignKey(AjusteInventario, on_delete=models.CASCADE, related_name="movimientos_inventario", null=True, blank=True)
    
    tipo_movimiento = models.CharField(max_length=50, choices=TipoMovimiento.choices, default=TipoMovimiento.ENTRADA)
    fecha_movimiento = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    observaciones = models.TextField(max_length=150, null=True, blank=True)

    recepcion = models.ForeignKey('compras.Recepcion', on_delete=models.CASCADE, null=True, blank=True)
    transferencia = models.ForeignKey('wms.Transferencia', on_delete=models.CASCADE, null=True, blank=True)
    op = models.ForeignKey('produccion.OrdenProduccion', on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        db_table = "movimientos_inventario"
        verbose_name = "Movimiento Inventario"
        verbose_name_plural = "Movimientos Inventario"

    def __str__(self):
        return str(self.id)
    
class MovimientoInventarioDetalle(models.Model):
    movimiento_inventario = models.ForeignKey(MovimientoInventario, on_delete=models.CASCADE, related_name="detalles")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="movimiento_inventario_detalle")
    producto_variante = models.ForeignKey('catalogo.ProductoVariante', on_delete=models.CASCADE, related_name='transferencia_detalle', blank=True, null=True)
    ubicacion_origen = models.ForeignKey(Ubicacion, on_delete=models.PROTECT, related_name="movimiento_inventario_detalle_origen", null=True, blank=True)
    ubicacion_destino = models.ForeignKey(Ubicacion, on_delete=models.PROTECT, related_name="movimiento_inventario_detalle_destino", null=True, blank=True)
    lote = models.ForeignKey(Lote, on_delete=models.PROTECT, related_name="movimiento_inventario_detalle", null=True, blank=True)
    serie = models.ForeignKey(Serie, on_delete=models.PROTECT, related_name="movimiento_inventario_detalle", null=True, blank=True)
    
    cantidad = models.DecimalField(max_digits=18, decimal_places=8, default=0)
    costo_unitario = models.DecimalField(max_digits=18, decimal_places=8, default=0)

    class Meta:
        db_table = "movimiento_inventario_detalle"
        verbose_name = "Movimiento Inventario Detalle"
        verbose_name_plural = "Movimientos Inventario Detalle"

    def __str__(self):
        return str(self.id)
