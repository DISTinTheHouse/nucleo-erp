from django.db import models
from nucleo.models import Empresa, Sucursal


class TipoAlmacen(models.TextChoices):
    MATERIA_PRIMA = "MP", "Materia Prima"
    PRODUCTO_TERMINADO = "PT", "Producto Terminado"
    EN_PROCESO = "PROCESO", "En Proceso"
    CUARENTENA = "CUARENTENA", "Cuarentena"
    DEVOLUCION = "DEVOLUCION", "Devolucion"
    TRANSITO = "TRANSITO", "Transito"

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
        return f"{self.sucursal.nombre} - {self.nombre}"


class TipoUbicacion(models.TextChoices):
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
