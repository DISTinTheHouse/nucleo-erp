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
    # Maquilero que confecciona la OP (``terceros.Proveedor`` con
    # ``es_maquilero``); ``null`` = confección interna.
    centro_confeccion = models.ForeignKey(
        'terceros.Proveedor', on_delete=models.PROTECT, null=True, blank=True,
        related_name='ordenes_produccion',
    )
    # Costo de confección por pieza en MXN (maquila externa o interna de LAZZAR):
    # se suma al costo de materiales del producto terminado que entra (#809).
    costo_maquila_unitario = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)

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

class OrdenProduccionPedido(models.Model):
    """Pedidos que cubre una OP (varios pedidos en una sola OP). ``OrdenProduccion.pedido``
    sigue siendo el pedido principal y siempre está aquí también."""

    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE, related_name='op_pedidos')
    pedido = models.ForeignKey(Pedido, on_delete=models.PROTECT, related_name='op_pedidos')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'orden_produccion_pedidos'
        verbose_name = 'Pedido de orden de producción'
        verbose_name_plural = 'Pedidos de orden de producción'
        constraints = [models.UniqueConstraint(fields=['op', 'pedido'], name='uq_op_pedido')]

    def __str__(self):
        return f"{self.op_id}-{self.pedido_id}"


class OrdenProduccionDetalle(models.Model):
    op_detalle_id = models.AutoField(primary_key=True)
    op = models.ForeignKey(OrdenProduccion, on_delete=models.CASCADE, related_name='orden_produccion_detalle')
    bom = models.ForeignKey(ListaMaterialBom, on_delete=models.CASCADE)
    cantidad = models.DecimalField(max_digits=12, decimal_places=2)
    unidad = models.ForeignKey(UnidadMedida, on_delete=models.PROTECT)
    observaciones = models.TextField(blank=True, null=True)
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE, null=True, blank=True)
    producto_variante = models.ForeignKey(ProductoVariante, on_delete=models.CASCADE, null=True, blank=True)
    # Muestra sin alta en catálogo (SKU ``MP…``): el renglón lleva la variante de
    # producción en lugar de ``producto_variante`` y su BOM sale de ella. Uno de
    # los dos, nunca ambos (lo valida ``OrdenProduccionDetalleSerializer``).
    variante_produccion = models.ForeignKey(
        VarianteProductoProduccion, on_delete=models.PROTECT, null=True, blank=True,
        related_name='ordenes_produccion_detalle',
    )

    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'ordenes_produccion_detalles'
        verbose_name = 'Orden Produccion Detalle'
        verbose_name_plural = 'Ordenes Produccion Detalles'

    def __str__(self):
        return str(self.op_detalle_id)

class PreOrdenProduccion(models.Model):
    """Pre-orden de producción de Fabricación especial (una abierta por pedido).

    Nace sola al generar/actualizar el BOM de una variante de producción de
    muestra (``produccion.services.pre_orden_service.sincronizar_pre_orden``) con
    las tallas que ya tienen BOM. Almacén confirma existencias; si faltan
    materiales se crea una OC en Borrador ligada (``OrdenCompra.pre_orden``). No
    aparta material: el estatus se recalcula al recibir la OC y al crear otras OP.
    """

    class Estatus(models.TextChoices):
        PENDIENTE_VALIDAR = "pendiente_validar", "Pendiente de validar"
        FALTAN_MATERIALES = "faltan_materiales", "Faltan materiales"
        EN_COMPRA = "en_compra", "En compra"
        LISTA = "lista", "Lista para producir"
        CONVERTIDA = "convertida", "Convertida en OP"
        CANCELADA = "cancelada", "Cancelada"

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE)
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT)
    pedido = models.ForeignKey(Pedido, on_delete=models.CASCADE, related_name="pre_ordenes_produccion")
    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.PENDIENTE_VALIDAR, db_index=True)
    op = models.ForeignKey(
        OrdenProduccion, on_delete=models.SET_NULL, null=True, blank=True, related_name="pre_ordenes",
    )
    fecha_confirmacion_existencias = models.DateTimeField(null=True, blank=True)
    usuario_confirma = models.ForeignKey(
        'usuarios.Usuario', on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # Seguimiento (los calcula ``pre_orden_service``; el front solo los muestra).
    estatus_desde = models.DateTimeField(null=True, blank=True)
    materiales_faltantes = models.PositiveIntegerField(default=0)
    regreso_fecha = models.DateTimeField(null=True, blank=True)
    regreso_motivo = models.CharField(max_length=255, blank=True, default="")
    # Ya se avisó que el semáforo está en rojo (se reinicia al salir de rojo).
    semaforo_rojo_avisado = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        db_table = "pre_ordenes_produccion"
        verbose_name = "Pre-orden Produccion"
        verbose_name_plural = "Pre-ordenes Produccion"
        constraints = [
            # Una sola abierta por pedido (convertidas y canceladas quedan de historial).
            models.UniqueConstraint(
                fields=["pedido"],
                condition=~models.Q(estatus__in=["convertida", "cancelada"]),
                name="uq_pre_orden_abierta_por_pedido",
            ),
        ]

    def __str__(self):
        return f"Pre-orden {self.pk} (pedido {self.pedido_id})"


class PreOrdenEvento(models.Model):
    """Línea de tiempo de la pre-orden (``pre-ordenes/{id}/historial``)."""

    class Evento(models.TextChoices):
        CREADA = "creada", "Creada"
        BOM_EDITADO = "bom_editado", "BOM editado"
        EXISTENCIAS_CONFIRMADAS = "existencias_confirmadas", "Existencias confirmadas"
        NECESIDAD_ENVIADA = "necesidad_enviada", "Necesidad enviada a compras"
        OC_CREADA = "oc_creada", "OC creada"
        OC_AUTORIZADA = "oc_autorizada", "OC autorizada"
        MATERIAL_RECIBIDO = "material_recibido", "Material recibido"
        REGRESO_CONSUMO = "regreso_consumo", "Regreso por consumo"
        LISTA = "lista", "Lista para producir"
        OP_CREADA = "op_creada", "OP creada"
        CANCELADA = "cancelada", "Cancelada"

    pre_orden = models.ForeignKey(PreOrdenProduccion, on_delete=models.CASCADE, related_name="eventos")
    fecha = models.DateTimeField(auto_now_add=True)
    evento = models.CharField(max_length=30, choices=Evento.choices)
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    descripcion = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "pre_ordenes_produccion_eventos"
        verbose_name = "Pre-orden Produccion Evento"
        verbose_name_plural = "Pre-ordenes Produccion Eventos"
        indexes = [models.Index(fields=["pre_orden", "fecha"], name="ix_pre_orden_evento_fecha")]


class PreOrdenProduccionDetalle(models.Model):
    """Talla de muestra con BOM: su variante de producción y las piezas."""

    pre_orden = models.ForeignKey(PreOrdenProduccion, on_delete=models.CASCADE, related_name="detalles")
    variante_produccion = models.ForeignKey(VarianteProductoProduccion, on_delete=models.PROTECT, related_name="+")
    pedido_detalle = models.ForeignKey(PedidoDetalle, on_delete=models.CASCADE, related_name="+")
    cantidad = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        db_table = "pre_ordenes_produccion_detalles"
        verbose_name = "Pre-orden Produccion Detalle"
        verbose_name_plural = "Pre-ordenes Produccion Detalle"
        constraints = [
            models.UniqueConstraint(fields=["pre_orden", "variante_produccion"], name="uq_pre_orden_variante"),
        ]


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
    fecha_kit_completo = models.DateTimeField(null=True, blank=True)
    fecha_embarque_materia_prima = models.DateField(null=True, blank=True)
    fecha_llegada_centro_confeccion = models.DateField(null=True, blank=True)

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

    # Checkbox -> fecha "marcado desde". La regla vive aquí, no en el serializer, para
    # que ORM, admin y scripts también la cumplan (#425).
    CAMPOS_ESTADO_CON_FECHA = {
        "existencia_tela": "fecha_existencia_tela",
        "sin_existencia_tela": "fecha_sin_existencia_tela",
        "existencia_avios": "fecha_existencia_avios",
        "sin_existencia_avios": "fecha_sin_existencia_avios",
        "kit_completo": "fecha_kit_completo",
    }

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._estado_original = {
            campo: instance.__dict__.get(campo) for campo in cls.CAMPOS_ESTADO_CON_FECHA
        }
        return instance

    def save(self, *args, **kwargs):
        """Sella la fecha al pasar el checkbox a true y la limpia al pasar a false.

        Solo cuando el valor cambia (o en el alta): un renglón anterior con el checkbox
        en true y sin fecha no se sella en silencio con la fecha de hoy.
        """
        from django.utils import timezone

        original = getattr(self, "_estado_original", None)
        tocados = []
        for campo, campo_fecha in self.CAMPOS_ESTADO_CON_FECHA.items():
            nuevo = getattr(self, campo)
            previo = original.get(campo) if original is not None else False
            if previo is None or nuevo == previo:
                continue
            setattr(self, campo_fecha, timezone.now() if nuevo else None)
            tocados.append(campo_fecha)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and tocados:
            kwargs["update_fields"] = {*update_fields, *tocados}
        super().save(*args, **kwargs)
        self._estado_original = {campo: getattr(self, campo) for campo in self.CAMPOS_ESTADO_CON_FECHA}

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
    
class _EstatusPrevioMixin:
    """Guarda en ``estatus_previo`` el paso en que estaba la orden al pasar a
    Detenido (6 en los tres enums); se limpia al salir de Detenido."""

    ESTATUS_CAMPO = None
    DETENIDO = 6

    @classmethod
    def from_db(cls, db, field_names, values):
        obj = super().from_db(db, field_names, values)
        obj._estatus_cargado = getattr(obj, cls.ESTATUS_CAMPO, None) if cls.ESTATUS_CAMPO in field_names else None
        return obj

    def save(self, *args, **kwargs):
        actual = getattr(self, self.ESTATUS_CAMPO)
        antes = getattr(self, "_estatus_cargado", None)
        campos = kwargs.get("update_fields")
        if antes is not None and actual != antes:
            if actual == self.DETENIDO:
                self.estatus_previo = antes
            elif antes == self.DETENIDO:
                self.estatus_previo = None
            if campos is not None:
                kwargs["update_fields"] = list(set(campos) | {"estatus_previo"})
        super().save(*args, **kwargs)
        self._estatus_cargado = actual


class OrdenesBordado(_EstatusPrevioMixin, StatusLifecycleModel):
    ESTATUS_CAMPO = "estatus_bordado"

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
    # Paso en que se quedó al pasar a Detenido (``_EstatusPrevioMixin``).
    estatus_previo = models.IntegerField(null=True, blank=True, choices=EstatusBordado.choices)
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
    #: Nulo en las líneas de MUESTRA con SKU de producción (sin producto de catálogo).
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, null=True, blank=True)
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

class OrdenesReflejante(_EstatusPrevioMixin, StatusLifecycleModel):
    ESTATUS_CAMPO = "estatus_reflejante"
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
    estatus_previo = models.IntegerField(null=True, blank=True, choices=EstatusReflejante.choices)
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

class OrdenesCorteManga(_EstatusPrevioMixin, StatusLifecycleModel):
    ESTATUS_CAMPO = "estatus_corte"
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
    estatus_previo = models.IntegerField(null=True, blank=True, choices=EstatusCorte.choices)
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


class CorteMangaAvances(StatusLifecycleModel):
    """Avance de piezas de una orden de corte de manga. Mismo patrón que
    ``ReflejanteAvances``: sólo piezas, sin renglón ni talla."""

    ocm = models.ForeignKey(OrdenesCorteManga, on_delete=models.CASCADE)
    fecha = models.DateTimeField(auto_now_add=True)
    cantidad = models.FloatField()
    usuario = models.ForeignKey('usuarios.Usuario', on_delete=models.CASCADE)
    comentario = models.TextField(blank=True, null=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = 'corte_manga_avances'
        verbose_name = 'Corte Manga Avance'
        verbose_name_plural = 'Corte Manga Avances'

    def __str__(self):
        return str(self.id)


class SalidaMaquila(models.Model):
    """SM: salida de prendas a un centro de confección externo (maquilero).

    Documento con folio SM, ligado a la OP y al maquilero, con cantidad y precio
    de maquila por renglón. NO mueve inventario: la materia prima ya la consumió
    la OP al crearse; la SM registra qué se mandó, a quién y a qué precio hasta
    que WMS recibe la producción y pasa calidad (``cerrar``).
    """

    class Estatus(models.TextChoices):
        ENVIADA = "ENVIADA", "Enviada"
        RECIBIDA = "RECIBIDA", "Recibida"
        CANCELADA = "CANCELADA", "Cancelada"

    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="salidas_maquila")
    sucursal = models.ForeignKey(Sucursal, on_delete=models.PROTECT, related_name="salidas_maquila")
    op = models.ForeignKey(OrdenProduccion, on_delete=models.PROTECT, related_name="salidas_maquila")
    centro_confeccion = models.ForeignKey(
        "terceros.Proveedor", on_delete=models.PROTECT, related_name="salidas_maquila"
    )
    folio = models.CharField(max_length=50)
    estatus = models.CharField(max_length=12, choices=Estatus.choices, default=Estatus.ENVIADA)
    fecha_envio = models.DateField()
    fecha_compromiso = models.DateField(null=True, blank=True)
    fecha_recepcion = models.DateTimeField(null=True, blank=True)
    total_piezas = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    observaciones = models.TextField(blank=True, default="")
    usuario = models.ForeignKey("usuarios.Usuario", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "salidas_maquila"
        verbose_name = "Salida a maquila"
        verbose_name_plural = "Salidas a maquila"
        constraints = [models.UniqueConstraint(fields=["empresa", "folio"], name="uq_sm_empresa_folio")]
        indexes = [models.Index(fields=["empresa", "-id"], name="sm_empresa_id")]

    def __str__(self):
        return self.folio


class SalidaMaquilaDetalle(models.Model):
    salida = models.ForeignKey(SalidaMaquila, on_delete=models.CASCADE, related_name="detalles")
    op_detalle = models.ForeignKey(
        OrdenProduccionDetalle, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    descripcion = models.CharField(max_length=255)
    cantidad = models.DecimalField(max_digits=14, decimal_places=2)
    precio_unitario = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    importe = models.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        db_table = "salidas_maquila_detalle"
        verbose_name = "Renglón de salida a maquila"
        verbose_name_plural = "Renglones de salida a maquila"
