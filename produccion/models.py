from django.db import models
from nucleo.models import Empresa, Sucursal, StatusLifecycleModel
from catalogo.models import Producto
from ventas.models import Pedido, PedidoDetalle
from inventarios.models import Almacen, Ubicacion

class ListaMaterialBom(models.Model):
    bom_id = models.AutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)

    class Meta:
        db_table = 'listas_materiales_bom'
        verbose_name = 'Lista Material bom'
        verbose_name_plural = 'Listas Materiales bom'
    
    def __str__(self):
        return str(self.bom_id)

class RutaProduccion(models.Model):
    ruta_produccion_id = models.AutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)

    class Meta:
        db_table = 'rutas_produccion'
        verbose_name = 'Ruta Produccion'
        verbose_name_plural = 'Rutas Produccion'
    
    def __str__(self):
        return str(self.ruta_produccion_id)
    
class OrdenProduccion(models.Model):
    op_id = models.AutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE)
    ruta_produccion = models.ForeignKey(RutaProduccion, on_delete=models.CASCADE)

    class Meta:
        db_table = 'ordenes_produccion'
        verbose_name = 'Orden Produccion'
        verbose_name_plural = 'Ordenes Produccion'
    
    def __str__(self):
        return str(self.op_id)

class ConsumoProduccion(models.Model):
    consumo_produccion_id = models.AutoField(primary_key=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE)

    class Meta:
        db_table = 'consumos_produccion'
        verbose_name = 'Consumo Produccion'
        verbose_name_plural = 'Consumos Produccion'

    def __str__(self):
        return str(self.consumo_produccion_id)

class ProductoTerminadoEntradas(models.Model):
    pt_entrada_id = models.AutoField(primary_key=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE)
    almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE)
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.CASCADE)

    class Meta:
        db_table = 'producto_terminado_entradas'
        verbose_name = 'Producto Terminado Entrada'
        verbose_name_plural = 'Producto Terminado Entradas'

    def __str__(self):
        return str(self.pt_entrada_id)
    
class OrdenesBordado(StatusLifecycleModel):
    class EstatusBordado(models.IntegerChoices):
        PENDIENTE = 1, "Pendiente"
        PREPARACION = 2, "Preparacion"
        BORDANDO = 3, "Bordando"
        REVISION = 4, "Revision"
        COMPLETADO = 5, "Completado"
        DETENIDO = 6, "Detenido"
        CANCELADO = 7, "Cancelado"
            
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE)
    folio_bordado = models.CharField(max_length=50, unique=True)
    estatus_bordado = models.IntegerField(default=EstatusBordado.PENDIENTE.value, choices=EstatusBordado.choices)
    prioridad = models.IntegerField()
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    usuario_asignado = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    observaciones = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'orden_bordado'
        verbose_name = 'Orden Bordado'
        verbose_name_plural = 'Ordenes Bordado'

    def __str__(self):
        return self.folio_bordado

class OrdenBordadoDetalle(models.Model):
    ob = models.ForeignKey(OrdenesBordado, on_delete=models.CASCADE)
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    cantidad = models.FloatField()
    posicion_bordado = models.CharField(max_length=50)
    colores_hilo = models.IntegerField(default=0)
    puntadas = models.IntegerField(default=0)

    class Meta:
        db_table = 'orden_bordado_detalle'
        verbose_name = 'Orden Bordado Detalle'
        verbose_name_plural = 'Ordenes Bordado Detalle'

    def __str__(self):
        return str(self.id)
    
class BordadoAvances(StatusLifecycleModel):
    ob = models.ForeignKey(OrdenesBordado, on_delete=models.CASCADE)
    fecha = models.DateTimeField(auto_now_add=True)
    cantidad_bordada = models.FloatField()
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    comentario = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'bordado_avances'
        verbose_name = 'Bordado Avance'
        verbose_name_plural = 'Bordado Avances'

    def __str__(self):
        return str(self.id)

class BordadoIncidencias(StatusLifecycleModel):
    ob = models.ForeignKey(OrdenesBordado, on_delete=models.CASCADE)
    tipo_incidencia = models.IntegerField()
    descripcion = models.TextField(blank=True, null=True)
    fecha = models.DateTimeField(auto_now_add=True)
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'bordado_incidencias'
        verbose_name = 'Bordado Incidencia'
        verbose_name_plural = 'Bordado Incidencias'

    def __str__(self):
        return str(self.id)

class OrdenesReflejante(StatusLifecycleModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE)
    folio_reflejante = models.CharField(max_length=50, unique=True)
    estatus_reflejante = models.IntegerField()
    prioridad = models.IntegerField()
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    observaciones = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'orden_reflejante'
        verbose_name = 'Orden Reflejante'
        verbose_name_plural = 'Ordenes Reflejante'

    def __str__(self):
        return self.folio_reflejante

class OrdenReflejanteDetalle(models.Model):
    orden_r = models.ForeignKey(OrdenesReflejante, on_delete=models.CASCADE)
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    cantidad = models.FloatField()
    tipo_reflejante = models.CharField(max_length=50)
    posicion = models.CharField(max_length=50)
    metros = models.FloatField()

    class Meta:
        db_table = 'orden_reflejante_detalle'
        verbose_name = 'Orden Reflejante Detalle'
        verbose_name_plural = 'Ordenes Reflejante Detalle'

    def __str__(self):
        return str(self.id)
    
class ReflejanteAvances(StatusLifecycleModel):
    orden_r = models.ForeignKey(OrdenesReflejante, on_delete=models.CASCADE)
    fecha = models.DateTimeField(auto_now_add=True)
    cantidad_aplicada = models.FloatField()
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    comentario = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'reflejante_avances'
        verbose_name = 'Reflejante Avance'
        verbose_name_plural = 'Reflejante Avances'

    def __str__(self):
        return str(self.id)

class ReflejanteIncidencias(StatusLifecycleModel):
    orden_r = models.ForeignKey(OrdenesReflejante, on_delete=models.CASCADE)
    descripcion = models.TextField(blank=True, null=True)
    fecha = models.DateTimeField(auto_now_add=True)
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'reflejante_incidencias'
        verbose_name = 'Reflejante Incidencia'
        verbose_name_plural = 'Reflejante Incidencias'

    def __str__(self):
        return str(self.id)