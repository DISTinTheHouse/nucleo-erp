from django.db import models
from nucleo.models import Empresa, Sucursal, StatusLifecycleModel
from catalogo.models import Producto, ProductoVariante, Talla, Color, UnidadMedida, VarianteProductoProduccion
from ventas.models import Pedido, PedidoDetalle
from inventarios.models import Almacen, Ubicacion

class ListaMaterialBom(models.Model):
    bom_id = models.AutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    producto_variante = models.ForeignKey(ProductoVariante, on_delete=models.CASCADE, null=True, blank=True)
    variante_produccion = models.ForeignKey(VarianteProductoProduccion, on_delete=models.CASCADE, null=True, blank=True) # Variante de produccion especial

    version = models.PositiveIntegerField(default=1)
    activo = models.BooleanField(default=True)
    observaciones = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'listas_materiales_bom'
        verbose_name = 'Lista Material bom'
        verbose_name_plural = 'Listas Materiales bom'
    
    def __str__(self):
        return str(self.bom_id)

class BomDetalle(models.Model):
    bom_detalle_id = models.AutoField(primary_key=True)
    bom = models.ForeignKey(ListaMaterialBom, on_delete=models.CASCADE, related_name='materia_prima_detalle')
    variante_produccion = models.ForeignKey(VarianteProductoProduccion, null=True, blank=True, on_delete=models.PROTECT) # Variante de produccion especial
    componente = models.ForeignKey(Producto, on_delete=models.PROTECT, null=True, blank=True, related_name='bom_componentes')
    cantidad = models.DecimalField(max_digits=12, decimal_places=2)
    unidad = models.ForeignKey(UnidadMedida, on_delete=models.PROTECT)
    desperdicio = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    obligatorio = models.BooleanField(default=True)
    observaciones = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)
    
    class Meta:
        db_table = 'bom_detalles'
        verbose_name = 'Bom Detalle'
        verbose_name_plural = 'Bom Detalles'

    def __str__(self):
        return str(self.bom_detalle_id)

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
    class EstatusOrdenProduccion(models.IntegerChoices):
        PENDIENTE = 1, "Pendiente"
        PREPARACION = 2, "Preparacion"
        BORDANDO = 3, "En produccion"
        REVISION = 4, "Revision"
        COMPLETADO = 5, "Completado"
        DETENIDO = 6, "Detenido"
        CANCELADO = 7, "Cancelado"

    op_id = models.AutoField(primary_key=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE)
    ruta_produccion = models.ForeignKey(RutaProduccion, on_delete=models.SET_NULL, null=True, blank=True)

    folio_op = models.CharField(max_length=50, unique=True)
    estatus_op = models.IntegerField(default=EstatusOrdenProduccion.PENDIENTE.value, choices=EstatusOrdenProduccion.choices)
    prioridad = models.IntegerField(default=1)
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    usuario_asignado = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE, null=True, blank=True)
    observaciones = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

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
    prioridad = models.IntegerField(default=1)
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    usuario_asignado = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE, null=True, blank=True)
    observaciones = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'orden_bordado'
        verbose_name = 'Orden Bordado'
        verbose_name_plural = 'Ordenes Bordado'

    def __str__(self):
        return self.folio_bordado

class OrdenBordadoDetalle(models.Model):
    ob = models.ForeignKey(OrdenesBordado, on_delete=models.CASCADE, related_name='detalles')
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    cantidad = models.FloatField()
    posicion_bordado = models.CharField(max_length=50, null=True, blank=True)
    colores_hilo = models.IntegerField(default=0)
    puntadas = models.IntegerField(default=0)
    talla = models.ForeignKey(Talla, on_delete=models.SET_NULL, null=True, blank=True)
    color = models.ForeignKey(Color, on_delete=models.SET_NULL, null=True, blank=True)

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
    class EstatusReflejante(models.IntegerChoices):
        PENDIENTE = 1, "Pendiente"
        PREPARACION = 2, "Preparacion"
        APLICANDO = 3, "Aplicando"
        REVISION = 4, "Revision"
        COMPLETADO = 5, "Completado"
        DETENIDO = 6, "Detenido"
        CANCELADO = 7, "Cancelado"

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE)
    folio_reflejante = models.CharField(max_length=50, unique=True)
    estatus_reflejante = models.IntegerField(default=EstatusReflejante.PENDIENTE, choices=EstatusReflejante.choices)
    prioridad = models.IntegerField(default=1)
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    usuario_asignado = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE, null=True, blank=True)
    observaciones = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'orden_reflejante'
        verbose_name = 'Orden Reflejante'
        verbose_name_plural = 'Ordenes Reflejante'

    def __str__(self):
        return self.folio_reflejante

class OrdenReflejanteDetalle(models.Model):
    orden_r = models.ForeignKey(OrdenesReflejante, on_delete=models.CASCADE, related_name='detalles')
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    cantidad = models.FloatField()
    tipo_reflejante = models.CharField(max_length=50, null=True, blank=True)
    posicion = models.CharField(max_length=50, null=True, blank=True)
    metros = models.FloatField(default=0)
    talla = models.ForeignKey(Talla, on_delete=models.SET_NULL, null=True, blank=True)
    color = models.ForeignKey(Color, on_delete=models.SET_NULL, null=True, blank=True)

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

class OrdenesCorteManga(StatusLifecycleModel):
    class EstatusCorte(models.IntegerChoices):
        PENDIENTE = 1, "Pendiente"
        PREPARACION = 2, "Preparacion"
        CORTANDO = 3, "Cortando"
        REVISION = 4, "Revision"
        COMPLETADO = 5, "Completado"
        DETENIDO = 6, "Detenido"
        CANCELADO = 7, "Cancelado"

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE)
    folio_ocm = models.CharField(max_length=50, unique=True)
    estatus_corte = models.IntegerField(default=EstatusCorte.PENDIENTE, choices=EstatusCorte.choices)
    prioridad = models.IntegerField(default=1)
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    usuario_asignado = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE, null=True, blank=True)
    observaciones = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'orden_corte_manga'
        verbose_name = 'Orden Corte Manga'
        verbose_name_plural = 'Ordenes Corte Manga'

    def __str__(self):
        return self.folio_ocm

class OrdenCorteMangaDetalle(models.Model):
    ocm = models.ForeignKey(OrdenesCorteManga, on_delete=models.CASCADE, related_name='detalles')
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    cantidad = models.FloatField()
    talla = models.ForeignKey(Talla, on_delete=models.SET_NULL, null=True, blank=True)
    color = models.ForeignKey(Color, on_delete=models.SET_NULL, null=True, blank=True)
    configuracion = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'orden_corte_manga_detalle'
        verbose_name = 'Orden Corte Manga Detalle'
        verbose_name_plural = 'Ordenes Corte Manga Detalle'

    def __str__(self):
        return str(self.id)