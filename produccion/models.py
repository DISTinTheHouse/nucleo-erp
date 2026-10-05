from django.contrib.postgres.indexes import OpClass
from django.db import models
from django.db.models.functions import Upper
from nucleo.indices import GinIndexSoloPostgres
from nucleo.models import Empresa, Sucursal, StatusLifecycleModel
from catalogo.models import Producto, ProductoVariante, Talla, Color, UnidadMedida, VarianteProductoProduccion
from ventas.models import Pedido, PedidoDetalle
from inventarios.models import Almacen, Ubicacion
from simple_history.models import HistoricalRecords

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
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, null=True, blank=True)
    ruta_produccion = models.ForeignKey(RutaProduccion, on_delete=models.SET_NULL, null=True, blank=True)

    folio_op = models.CharField(max_length=50, unique=True)
    estatus_op = models.IntegerField(default=EstatusOrdenProduccion.PENDIENTE.value, choices=EstatusOrdenProduccion.choices)
    prioridad = models.IntegerField(default=1)
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    fecha_entrega_estimada = models.DateField(
        null=True, blank=True,
        help_text="Fecha estimada de entrega/resurtido, capturada por producción (mismo criterio que OrdenCompra.fecha_entrega_estimada)."
    )
    usuario_asignado = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE, null=True, blank=True)
    observaciones = models.TextField(blank=True, null=True)
    cerrar_orden = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        db_table = 'ordenes_produccion'
        verbose_name = 'Orden Produccion'
        verbose_name_plural = 'Ordenes Produccion'
    
    def __str__(self):
        return str(self.op_id)

class OrdenProduccionDetalle(models.Model):
    op_detalle_id = models.AutoField(primary_key=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE, related_name='orden_produccion_detalle')
    bom = models.ForeignKey(ListaMaterialBom, on_delete=models.CASCADE)
    cantidad = models.DecimalField(max_digits=12, decimal_places=2)
    unidad = models.ForeignKey(UnidadMedida, on_delete=models.PROTECT)
    observaciones = models.TextField(blank=True, null=True)
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE, null=True, blank=True)
    producto_variante = models.ForeignKey(ProductoVariante, on_delete=models.CASCADE, null=True, blank=True)

    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'ordenes_produccion_detalles'
        verbose_name = 'Orden Produccion Detalle'
        verbose_name_plural = 'Ordenes Produccion Detalles'

    def __str__(self):
        return str(self.op_detalle_id)

class OrdenProduccionRutaCritica(models.Model):
    """Seguimiento de ruta crítica de la OP: un renglón por orden (no por
    línea/variante), pensado para que mesa de control/producción actualice
    estatus con PATCH parciales y rápidos sin tocar el serializer pesado de
    ``OrdenProduccion`` (que arrastra BOM/detalles anidados).

    Cubre los subprocesos: desarrollo de producto, telas & avíos, trazo,
    corte y producción. El subproceso de compras queda pendiente hasta
    nuevo aviso (sin campos por ahora).
    """

    class EstatusPaqueteTecnico(models.TextChoices):
        ACTUALIZADA = "actualizada", "Actualizada"
        NUEVA = "nueva", "Nueva"
        ESPERA_MUESTRA = "espera_muestra", "En espera de muestra"

    op = models.OneToOneField(
        OrdenProduccion, on_delete=models.CASCADE, primary_key=True, related_name="ruta_critica"
    )

    # 1. Desarrollo de producto
    fecha_liberacion_paquete_tecnico = models.DateField(null=True, blank=True)
    estatus_paquete_tecnico = models.CharField(
        max_length=20, choices=EstatusPaqueteTecnico.choices, null=True, blank=True
    )

    # 2. Telas & avíos. ``existencia_*``/``sin_existencia_*`` son dos booleans
    # independientes (no un tri-estado): cada uno lleva su propia fecha,
    # sellada por el servidor (nunca por el cliente) cuando su valor cambia.
    existencia_tela = models.BooleanField(default=False)
    fecha_existencia_tela = models.DateTimeField(null=True, blank=True)
    sin_existencia_tela = models.BooleanField(default=False)
    fecha_sin_existencia_tela = models.DateTimeField(null=True, blank=True)
    existencia_avios = models.BooleanField(default=False)
    fecha_existencia_avios = models.DateTimeField(null=True, blank=True)
    sin_existencia_avios = models.BooleanField(default=False)
    fecha_sin_existencia_avios = models.DateTimeField(null=True, blank=True)
    fecha_real_surtido_telas = models.DateField(null=True, blank=True)
    fecha_real_surtido_avios = models.DateField(null=True, blank=True)
    corte_externo = models.BooleanField(default=False)
    comentarios_telas_avios = models.TextField(blank=True, null=True)
    kit_completo = models.BooleanField(default=False)
    fecha_embarque_materia_prima = models.DateField(null=True, blank=True)

    # 4. Trazo
    fecha_trazo = models.DateField(null=True, blank=True)

    # 5. Corte
    fecha_real_corte = models.DateField(null=True, blank=True)
    cantidad_real_corte = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # 6. Producción
    corte_recibido = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ordenes_produccion_ruta_critica'
        verbose_name = 'Ruta Crítica de Orden de Producción'
        verbose_name_plural = 'Rutas Críticas de Órdenes de Producción'

    def __str__(self):
        return f"Ruta crítica OP {self.op_id}"

class ConsumoProduccion(models.Model):
    consumo_produccion_id = models.AutoField(primary_key=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE)

    class Meta:
        db_table = 'consumos_produccion'
        verbose_name = 'Consumo Produccion'
        verbose_name_plural = 'Consumos Produccion'

    def __str__(self):
        return str(self.consumo_produccion_id)

class ConsumoProduccionDetalle(models.Model):
    consumo_detalle_id = models.AutoField(primary_key=True)
    consumo_produccion = models.ForeignKey(
        ConsumoProduccion,
        on_delete=models.CASCADE,
        related_name='detalles',
    )
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)
    cantidad = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        db_table = 'consumo_detalle'
        verbose_name = 'Consumo Produccion Detalle'
        verbose_name_plural = 'Consumos Produccion Detalle'

    def __str__(self):
        return str(self.consumo_detalle_id)

class ProductoTerminadoEntradas(models.Model):
    pt_entrada_id = models.AutoField(primary_key=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE)
    almacen = models.ForeignKey(Almacen, on_delete=models.CASCADE)
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.CASCADE)

    history = HistoricalRecords()

    class Meta:
        db_table = 'producto_terminado_entradas'
        verbose_name = 'Producto Terminado Entrada'
        verbose_name_plural = 'Producto Terminado Entradas'

    def __str__(self):
        return str(self.pt_entrada_id)
    
class OrdenesBordado(StatusLifecycleModel):
    class EstatusBordado(models.IntegerChoices):
        SIN_TRABAJAR = 1, "Sin trabajar"
        PROGRAMADO = 2, "Programado"
        PONCHADO = 3, "Ponchado"
        ARREGLO = 4, "Arreglo"
        BORDANDO = 5, "Bordando"
        DETENIDO = 6, "Detenido"
        FINALIZADO = 7, "Finalizado"
        CANCELADO_LEGACY = 8, "Cancelado (legacy)"
            
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.CASCADE)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE)
    folio_bordado = models.CharField(max_length=50, unique=True)
    estatus_bordado = models.IntegerField(default=EstatusBordado.SIN_TRABAJAR.value, choices=EstatusBordado.choices)
    prioridad = models.IntegerField(default=1)  
    fecha_inicio = models.DateTimeField(auto_now_add=True)
    fecha_fin = models.DateTimeField(null=True, blank=True)
    usuario_asignado = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE, null=True, blank=True)
    observaciones = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)
    maquina_asignada = models.CharField(max_length=100, null=True, blank=True)
    proveedor = models.ForeignKey(
        "terceros.Proveedor",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ordenes_bordado_proveedor",
    )

    class Meta:
        db_table = 'orden_bordado'
        verbose_name = 'Orden Bordado'
        verbose_name_plural = 'Ordenes Bordado'
        indexes = [
            # Buscador global (``/api/v1/search/``): el folio se busca por
            # subcadena (``folio_bordado__icontains``, el año va delante del
            # consecutivo), que se compila como
            # ``UPPER("folio_bordado"::text) LIKE UPPER('%...%')``. Ni el índice
            # único del campo ni su ``_like`` sirven a eso; un GIN trigram sobre
            # ``UPPER(col)`` sí. Mismo patrón que ``ventas.Pedido`` (ver
            # ``nucleo/api/search.py``). Sólo existe en PostgreSQL: ver
            # ``nucleo.indices``.
            GinIndexSoloPostgres(
                OpClass(Upper("folio_bordado"), name="gin_trgm_ops"),
                name="orden_bordado_folio_up_trgm",
            ),
        ]

    def __str__(self):
        return self.folio_bordado

class OrdenBordadoDetalle(models.Model):
    class TipoServicioBordadoLegacy(models.TextChoices):
        """Opciones single-pick legacy del renglón del detalle.

        Conservado para no perder data de migración 0032 aplicada en
        ciclos anteriores de QA.
        """
        PLANO = "Plano", "Bordado Plano"
        TRES_D = "3D", "Bordado 3D (Puff)"
        CHENILLE = "Chenille", "Chenille / Toalla"
        APLICACION = "Aplicacion", "Aplicación / Parche"
        REFLECTANTE = "Reflectante", "Hilo Reflectante"
        METALICO = "Metalico", "Hilo Metálico"
        COMBINADO = "Combinado", "Combinado (2+ técnicas)"
        OTRO = "Otro", "Otro (ver descripción)"

    from ventas.servicios_bordado import TipoServicioBordado as _TipoServicioBordadoCentral
    TipoServicioBordado = _TipoServicioBordadoCentral

    ob = models.ForeignKey(OrdenesBordado, on_delete=models.CASCADE, related_name='detalles')
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE)
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE)
    cantidad = models.FloatField()
    posicion_bordado = models.CharField(max_length=50, null=True, blank=True)
    colores_hilo = models.IntegerField(default=0)
    puntadas = models.IntegerField(default=0)
    talla = models.ForeignKey(Talla, on_delete=models.SET_NULL, null=True, blank=True)
    color = models.ForeignKey(Color, on_delete=models.SET_NULL, null=True, blank=True)
    tipo_servicio_bordado = models.CharField(
        max_length=40,
        choices=TipoServicioBordadoLegacy.choices,
        default=TipoServicioBordadoLegacy.PLANO,
    )
    descripcion_servicio = models.TextField(blank=True, null=True)
    tipos_servicio = models.JSONField(default=list, blank=True)
    #: ``bordado_config`` ÍNTEGRO de la ``PedidoDetalleTalla`` de origen.
    #:
    #: Los tres escalares de arriba se derivan de ``ubicaciones[0]``, así que
    #: una línea con varias ubicaciones perdía todas menos la primera —y hay
    #: filas reales con 2—. Aquí se guarda el objeto completo, con todo
    #: ``ubicaciones[]``, para que el renglón deje de ser el único registro y
    #: pase a ser el atajo. Mismo patrón que ``OrdenCorteMangaDetalle
    #: .configuracion``. Nulo en las órdenes anteriores a este campo: no se
    #: hizo backfill.
    configuracion = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'orden_bordado_detalle'
        verbose_name = 'Orden Bordado Detalle'
        verbose_name_plural = 'Ordenes Bordado Detalle'

    def __str__(self):
        return str(self.id)
    
class BordadoAvances(StatusLifecycleModel):
    ob = models.ForeignKey(OrdenesBordado, on_delete=models.CASCADE)
    orden_bordado_detalle = models.ForeignKey(
        OrdenBordadoDetalle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    pedido_detalle_talla = models.ForeignKey(
        "ventas.PedidoDetalleTalla",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    fecha = models.DateTimeField(auto_now_add=True)
    cantidad_bordada = models.FloatField()
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    comentario = models.TextField(blank=True, null=True)
    puntadas_por_pieza = models.IntegerField(default=0)
    puntadas_realizadas = models.IntegerField(default=0)
    puntadas_total = models.IntegerField(default=0)
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
        indexes = [
            # Buscador global: ``folio_reflejante__icontains`` (el año va delante
            # del consecutivo) se compila como
            # ``UPPER("folio_reflejante"::text) LIKE UPPER('%...%')``. Mismo
            # patrón que ``OrdenesBordado``. Sólo existe en PostgreSQL: ver
            # ``nucleo.indices``.
            GinIndexSoloPostgres(
                OpClass(Upper("folio_reflejante"), name="gin_trgm_ops"),
                name="orden_refl_folio_up_trgm",
            ),
        ]

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
    #: ``reflejante_config`` ÍNTEGRO de la ``PedidoDetalleTalla`` de origen.
    #:
    #: A diferencia de bordado y corte de manga, el config de reflejante ES el
    #: arreglo (``[{"tipo", "opcion", "posicion"}, ...]``), no un objeto que lo
    #: envuelve. Los escalares de arriba se derivan del elemento ``[0]``, así
    #: que una línea con varios elementos perdía el resto —y en datos reales no
    #: sólo se pierde una posición: P-00027 mezcla ``ignifuga-plata-1`` con
    #: ``costurable-plata-1``, o sea un MATERIAL distinto—. Aquí se guarda el
    #: arreglo completo. Mismo patrón que ``OrdenCorteMangaDetalle
    #: .configuracion``. Nulo en las órdenes anteriores: no se hizo backfill.
    configuracion = models.JSONField(null=True, blank=True)

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
        indexes = [
            # Buscador global: ``folio_ocm__icontains`` (el año va delante del
            # consecutivo) se compila como
            # ``UPPER("folio_ocm"::text) LIKE UPPER('%...%')``. Mismo patrón que
            # ``OrdenesBordado``. Sólo existe en PostgreSQL: ver ``nucleo.indices``.
            GinIndexSoloPostgres(
                OpClass(Upper("folio_ocm"), name="gin_trgm_ops"),
                name="orden_cm_folio_up_trgm",
            ),
        ]

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
