from decimal import Decimal

from wms.utils.decimales import normalizar_decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from catalogo.models import Producto, ProductoVariante
from inventarios.models import Almacen
from logistica.models import Envio
from hr.models import Empleado
from nucleo.listados import nombre_para_mostrar_usuario as _nombre_usuario
from wms.models import (
    Despacho,
    DespachoDetalle,
    EtiquetaRFIDDetalle,
    EtiquetaRFIDImpresion,
    Packing,
    PackingDetalle,
    Picking,
    PickingDetalle,
    PickingOrdenTrabajo,
    Transferencia,
    TransferenciaDetalle,
)
from ventas.services.pedido_vinculado import armar_pedido_vinculado
from wms.services.rfid_label_service import zpl_rfid_vigente


class _CanceladoPorNombreMixin(serializers.Serializer):
    """``cancelado_por_nombre`` (EC-949): quién canceló, con el mismo fallback que
    ``usuario_nombre``; ``null`` si el documento no está cancelado. Misma regla que
    Compras (EC-926). ``cancelado_por`` viaja en el ``select_related`` del viewset."""

    cancelado_por_nombre = serializers.SerializerMethodField()

    @extend_schema_field({"type": "string", "nullable": True})
    def get_cancelado_por_nombre(self, obj):
        return _nombre_usuario(obj.cancelado_por) if obj.cancelado_por_id else None


class TransferenciaDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransferenciaDetalle
        fields = "__all__"
        read_only_fields = ["transferencia"]

    def validate(self, attrs):
        producto = attrs.get("producto")
        producto_variante = attrs.get("producto_variante")

        if not producto and not producto_variante:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Debe proporcionar 'producto' o 'producto_variante'."
                    ]
                }
            )

        if producto and producto_variante:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "Solo puede proporcionar 'producto' o 'producto_variante'."
                    ]
                }
            )

        return attrs


class TransferenciaSerializer(serializers.ModelSerializer):
    transferencia_detalle = TransferenciaDetalleSerializer(many=True)

    class Meta:
        model = Transferencia
        fields = "__all__"
        # ``picking`` lo fija el servidor (EC-916): una TR manual no se liga a un picking.
        read_only_fields = ["empresa", "sucursal", "folio", "usuario", "status", "picking", "reversa_de",
                            "motivo_cancelacion", "fecha_cancelacion", "cancelado_por"]

    def validate(self, attrs):
        almacen_origen = attrs.get("almacen_origen")
        almacen_destino = attrs.get("almacen_destino")

        if almacen_origen == almacen_destino:
            raise serializers.ValidationError(
                {
                    "non_field_errors": [
                        "El almacen de origen y destino no pueden ser iguales."
                    ]
                }
            )

        return attrs


class _VarianteDeLineaWmsMixin(serializers.Serializer):
    """Producto, ``sku``, color y talla de un renglón de WMS (EC-882), con una sola fuente.

    ``sku``, ``color_*`` y ``talla_*`` salen SIEMPRE de la misma variante, la que se
    surtió en la línea: su ``producto_variante`` (catálogo) o, si no tiene, su
    ``variante_produccion`` (muestra, con su propio ``sku``, ``color`` y ``talla``).
    Nunca de la variante que hoy tenga la talla del pedido, que puede cambiar
    después del picking. Sólo si la línea no tiene ninguna de las dos, ``talla_*``
    cae a la talla del pedido (``pedido_detalle_talla.talla``; la transferencia no
    tiene) y ``sku``/``color_*`` quedan en ``null``. Una línea con las dos variantes
    da el ``sku`` de catálogo y ``sku_muestra`` el de la muestra: pueden diferir.

    ``producto_nombre``: el del producto de la línea o, sin producto, el de su
    muestra; ``null`` si no hay ninguno o el nombre viene vacío.

    ``_linea_wms`` devuelve el objeto con esas FK: el propio renglón en picking y
    transferencia, su ``picking_detalle`` en packing. Quien lo usa precarga
    ``producto``, ``producto_variante__color``/``__talla``,
    ``variante_produccion__color``/``__talla`` y, en picking y packing,
    ``pedido_detalle_talla__talla`` (sin consultas por fila).
    """

    producto_nombre = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()
    color_id = serializers.SerializerMethodField()
    color_nombre = serializers.SerializerMethodField()
    talla_id = serializers.SerializerMethodField()
    talla_nombre = serializers.SerializerMethodField()

    def _linea_wms(self, obj):
        return obj

    def _variante_wms(self, obj):
        linea = self._linea_wms(obj)
        if linea.producto_variante_id:
            return linea.producto_variante
        if linea.variante_produccion_id:
            return linea.variante_produccion
        return None

    def _color_wms(self, obj):
        variante = self._variante_wms(obj)
        return variante.color if variante is not None and variante.color_id else None

    def _talla_wms(self, obj):
        variante = self._variante_wms(obj)
        if variante is not None:
            return variante.talla if variante.talla_id else None
        talla_pedido = getattr(self._linea_wms(obj), "pedido_detalle_talla", None)
        return talla_pedido.talla if talla_pedido is not None else None

    def get_producto_nombre(self, obj):
        linea = self._linea_wms(obj)
        if linea.producto_id:
            return linea.producto.nombre or None
        return getattr(linea.variante_produccion, "nombre", None) or None

    def get_sku(self, obj):
        return getattr(self._variante_wms(obj), "sku", None) or None

    def get_color_id(self, obj):
        return getattr(self._color_wms(obj), "pk", None)

    def get_color_nombre(self, obj):
        return getattr(self._color_wms(obj), "nombre", None)

    def get_talla_id(self, obj):
        return getattr(self._talla_wms(obj), "pk", None)

    def get_talla_nombre(self, obj):
        return getattr(self._talla_wms(obj), "nombre", None)


class TransferenciaDetalleReadSerializer(_VarianteDeLineaWmsMixin, serializers.ModelSerializer):
    """Renglón anidado del ``retrieve`` individual de una transferencia.

    Resuelve los nombres a través de las FK que el ``prefetch_related`` del viewset
    ya trae con ``select_related`` — sin consultas por fila.

    ``producto`` y ``producto_variante`` son mutuamente excluyentes (lo garantiza
    ``TransferenciaDetalleSerializer.validate``), así que exactamente uno de
    ``producto_nombre``/``producto_variante_nombre`` resuelve y el otro queda en
    ``null``.

    ``ubicacion_origen``/``ubicacion_destino`` se resuelven con ``str(ubicacion)``
    —siguiendo la convención de ``reporte-movimientos-periodo``— porque
    ``Ubicacion`` NO tiene campo ``nombre``: su etiqueta se compone de
    ``almacen.nombre`` + coordenadas (pasillo-rack-nivel-posicion), de ahí el
    ``select_related`` de ``ubicacion_*__almacen`` en el viewset.

    ``lote``/``serie`` se exponen solo como id: ambos modelos únicamente tienen
    ``id`` y una FK a ``producto``, sin ningún campo identificador que resolver.

    Línea de muestra (EC-882): sin ``producto`` ni ``producto_variante``, la línea
    trae ``variante_produccion`` (la crea así el picking de una talla con SKU de
    muestra). ``producto_nombre`` cae entonces al nombre de esa variante —misma
    regla que ``PickingDetalleSerializer``— y ``sku_muestra`` lleva su SKU. ``sku``,
    ``color_*`` y ``talla_*`` salen de la variante de catálogo o de la de muestra
    (``_VarianteDeLineaWmsMixin``).
    """

    producto_variante_nombre = serializers.CharField(
        source="producto_variante.nombre", read_only=True, default=None
    )
    sku_muestra = serializers.CharField(source="variante_produccion.sku", read_only=True, default=None)
    ubicacion_origen_nombre = serializers.SerializerMethodField()
    ubicacion_destino_nombre = serializers.SerializerMethodField()

    class Meta:
        model = TransferenciaDetalle
        fields = [
            "id",
            "producto",
            "producto_nombre",
            "producto_variante",
            "producto_variante_nombre",
            "variante_produccion",
            "sku_muestra",
            "sku",
            "color_id",
            "color_nombre",
            "talla_id",
            "talla_nombre",
            "cantidad",
            "ubicacion_origen",
            "ubicacion_origen_nombre",
            "ubicacion_destino",
            "ubicacion_destino_nombre",
            "lote",
            "serie",
        ]

    def get_ubicacion_origen_nombre(self, obj):
        return str(obj.ubicacion_origen) if obj.ubicacion_origen_id else None

    def get_ubicacion_destino_nombre(self, obj):
        return str(obj.ubicacion_destino) if obj.ubicacion_destino_id else None


class TransferenciaListSerializer(_CanceladoPorNombreMixin, serializers.ModelSerializer):
    """Listado de transferencias (acción ``list``).

    Forma ligera y plana: encabezado con las FK resueltas, sin anidar renglones
    —mismo criterio que ``RecepcionListSerializer`` vs. ``RecepcionRetrieveSerializer``
    en Compras—. ``almacen_origen``/``almacen_destino``/``usuario`` son NOT NULL en
    el modelo, así que sus nombres siempre resuelven.
    """

    almacen_origen_nombre = serializers.CharField(
        source="almacen_origen.nombre", read_only=True
    )
    almacen_destino_nombre = serializers.CharField(
        source="almacen_destino.nombre", read_only=True
    )
    usuario_nombre = serializers.SerializerMethodField()
    # Enlace original ↔ reversa (EC-949): la reversa apunta a la original con
    # ``reversa_de``; la original, a su reversa con ``reversa`` (``related_name``).
    # Ambas viajan en el ``select_related`` del viewset. ``null`` si no hay: sin
    # reversa, el acceso inverso lanza ``ObjectDoesNotExist`` y DRF lo vuelve ``None``.
    reversa_de_folio = serializers.CharField(
        source="reversa_de.folio", read_only=True, default=None, allow_null=True
    )
    reversa = serializers.IntegerField(source="reversa.id", read_only=True, default=None, allow_null=True)
    reversa_folio = serializers.CharField(source="reversa.folio", read_only=True, default=None, allow_null=True)

    class Meta:
        model = Transferencia
        fields = [
            "id",
            "folio",
            "status",
            "observaciones",
            "fecha_creacion",
            "almacen_origen",
            "almacen_origen_nombre",
            "almacen_destino",
            "almacen_destino_nombre",
            "usuario",
            "usuario_nombre",
            "reversa_de",
            "reversa_de_folio",
            "reversa",
            "reversa_folio",
            "motivo_cancelacion",
            "fecha_cancelacion",
            "cancelado_por",
            "cancelado_por_nombre",
        ]

    def get_usuario_nombre(self, obj):
        return _nombre_usuario(obj.usuario)


class TransferenciaRetrieveSerializer(TransferenciaListSerializer):
    """Detalle individual de una transferencia (acción ``retrieve``).

    Mismo encabezado que el listado más los renglones anidados.
    """

    transferencia_detalle = TransferenciaDetalleReadSerializer(
        many=True, read_only=True
    )

    class Meta(TransferenciaListSerializer.Meta):
        fields = TransferenciaListSerializer.Meta.fields + ["transferencia_detalle"]


class PickingDetalleSerializer(_VarianteDeLineaWmsMixin, serializers.ModelSerializer):
    """Renglón anidado de un picking.

    Resuelve los nombres a través de las FK que el ``prefetch_related`` del viewset
    ya trae con ``select_related`` — sin consultas por fila, misma convención que
    ``TransferenciaDetalleReadSerializer``.

    ``producto``/``producto_variante``: el que esté en ``null`` deja su
    ``*_nombre`` en ``null``.

    ``ubicacion`` se resuelve con ``str(ubicacion)`` porque ``Ubicacion`` NO tiene
    campo ``nombre``: su etiqueta se compone de ``almacen.nombre`` + coordenadas
    (pasillo-rack-nivel-posicion), de ahí el ``select_related`` de
    ``ubicacion__almacen`` en el viewset.

    ``lote`` (``inventarios.Lote``) se expone solo como id: el modelo únicamente
    tiene ``id`` y una FK a ``producto``, sin ningún campo identificador que
    resolver (misma convención que ``lote``/``serie`` en transferencias).

    ``oleada`` y ``lote_picking`` usan ``__str__`` null-safe como etiqueta porque
    sus modelos no tienen un campo ``nombre`` natural (su ``__str__`` compone id
    + estado), al igual que ``TransferenciaDetalleReadSerializer`` con ``Ubicacion``.

    ``sku``, ``color_id`` y ``color_nombre`` (EC-882) salen de la variante de
    catálogo o, en una línea de muestra, de su ``variante_produccion``
    (``_VarianteDeLineaWmsMixin``); ``sku_muestra`` sigue siendo sólo el SKU de la
    muestra.
    """

    producto_variante_nombre = serializers.CharField(
        source="producto_variante.nombre", read_only=True, default=None
    )
    # Línea de muestra: su SKU de muestra (``variante_produccion``).
    sku_muestra = serializers.CharField(source="variante_produccion.sku", read_only=True, default=None)
    ubicacion_nombre = serializers.SerializerMethodField()
    operador_nombre = serializers.SerializerMethodField()

    class Meta:
        model = PickingDetalle
        fields = "__all__"
        read_only_fields = [
            "picking",
            "producto",
            "producto_variante",
            "cantidad_solicitada",
        ]

    def get_ubicacion_nombre(self, obj):
        return str(obj.ubicacion) if obj.ubicacion_id else None

    def get_operador_nombre(self, obj):
        return _nombre_usuario(obj.operador)


class PickingOrdenTrabajoReadSerializer(serializers.ModelSerializer):
    """Renglón de vínculo entre un picking y una orden de trabajo generada.

    Se adjunta nested en el list/retrieve de ``Picking`` para que Next.js pueda
    recuperar la relación sin un endpoint adicional —la relación de Django se
    prefetchcea desde ``PickingViewSet.get_queryset``.
    """

    orden_bordado_folio = serializers.CharField(
        source="orden_bordado.folio_bordado", read_only=True, default=None
    )
    orden_reflejante_folio = serializers.CharField(
        source="orden_reflejante.folio_reflejante", read_only=True, default=None
    )
    orden_corte_manga_folio = serializers.CharField(
        source="orden_corte_manga.folio_ocm", read_only=True, default=None
    )
    tipo_orden_label = serializers.CharField(
        source="get_tipo_orden_display", read_only=True
    )

    class Meta:
        model = PickingOrdenTrabajo
        fields = [
            "id",
            "tipo_orden",
            "tipo_orden_label",
            "orden_bordado",
            "orden_bordado_folio",
            "orden_reflejante",
            "orden_reflejante_folio",
            "orden_corte_manga",
            "orden_corte_manga_folio",
        ]


class PickingSerializer(_CanceladoPorNombreMixin, serializers.ModelSerializer):
    """Serializer compartido de picking (``list``, ``retrieve`` y respuesta del
    ``create``).

    Además de las FK crudas expone los nombres resueltos —misma convención que
    ``TransferenciaListSerializer``—. Todos los ``*_nombre`` son de solo lectura,
    así que el contrato de escritura del ``POST /pickings/`` no cambia.

    ``pedido``/``operador``/``almacen``/``usuario`` son NOT NULL en el modelo;
    ``almacen_destino`` y ``transferencia`` son nullable (``trasladar`` deja el destino
    en ``null``) (``pedido_folio`` aun así puede ser ``null``: ``Pedido.folio`` es
    nullable). ``oleada``/``zona_almacen``/``lote`` son FK opcionales y resuelven
    a ``null`` cuando faltan. ``Oleada`` y ``LotePicking`` no tienen campo
    ``nombre``: su etiqueta se compone en ``__str__`` (id + estado), por eso se
    resuelven con ``str()`` null-safe, igual que ``Ubicacion`` en transferencias.

    ``ordenes_trabajo`` está nested y prefetchceado para que el GET recupere la
    trazabilidad contra producción; el POST además inyecta un arreglo plano
    ``ordenes_trabajo_generadas`` con resumen ``{tipo, id, folio}``.

    ``pedido_vinculado`` (EC-882) es ``{id, folio, cliente}`` con la misma forma y
    regla que los detalles de Producción (EC-880, ``armar_pedido_vinculado``): el
    cliente sale de la foto que guarda el pedido o, sin foto, del ``Cliente`` vivo
    (``pedido__cliente`` en el ``select_related`` del viewset). ``pedido`` y
    ``pedido_folio`` no cambian.
    """

    picking_detalle = PickingDetalleSerializer(many=True)
    ordenes_trabajo = PickingOrdenTrabajoReadSerializer(many=True, read_only=True)

    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True)
    pedido_vinculado = serializers.SerializerMethodField()
    operador_nombre = serializers.SerializerMethodField()
    almacen_nombre = serializers.CharField(source="almacen.nombre", read_only=True)
    almacen_destino_nombre = serializers.CharField(
        source="almacen_destino.nombre", read_only=True, default=None
    )
    usuario_nombre = serializers.SerializerMethodField()
    oleada_nombre = serializers.SerializerMethodField()
    zona_almacen_nombre = serializers.CharField(
        source="zona_almacen.nombre", read_only=True, default=None
    )
    lote_nombre = serializers.SerializerMethodField()
    transferencia_folio = serializers.CharField(
        source="transferencia.folio", read_only=True, default=None
    )
    # Dónde están hoy las prendas (``Picking.almacen_actual``, misma regla que el traslado).
    almacen_actual = serializers.PrimaryKeyRelatedField(read_only=True)
    almacen_actual_nombre = serializers.CharField(source="almacen_actual.nombre", read_only=True)
    # Empaque, avance de la programación y entrega del pedido. El listado los calcula
    # para toda la página (``contexto_filas`` en el contexto, ver ``PickingViewSet.list``);
    # sin él (retrieve, alta) se calculan para el picking.
    packings = serializers.SerializerMethodField()
    packing_folio = serializers.SerializerMethodField()
    estado_empaque = serializers.SerializerMethodField()
    programacion_avance = serializers.SerializerMethodField()
    pedido_fecha_entrega = serializers.SerializerMethodField()
    cliente_nombre = serializers.SerializerMethodField()

    def get_cliente_nombre(self, obj):
        pedido = obj.pedido
        if not pedido:
            return None
        return getattr(pedido.cliente, "nombre", None) or pedido.cliente_nombre

    def _ctx_fila(self, obj):
        from wms.api.pickings_listado import contexto_filas

        ctx = self.context.get("contexto_filas")
        if ctx is None or obj.pk not in ctx["empaque"]:
            nuevo = contexto_filas([obj])
            if ctx is None:
                ctx = self.context["contexto_filas"] = nuevo
            else:
                ctx["empaque"].update(nuevo["empaque"])
                ctx["surtido"].update(nuevo["surtido"])
        return ctx

    def get_packings(self, obj):
        return self._ctx_fila(obj)["empaque"][obj.pk]

    def get_packing_folio(self, obj):
        packings = self._ctx_fila(obj)["empaque"][obj.pk]
        return packings[-1]["folio"] if packings else None

    def get_estado_empaque(self, obj):
        from wms.api.pickings_listado import estado_empaque
        return estado_empaque(self._ctx_fila(obj)["empaque"][obj.pk])

    def get_programacion_avance(self, obj):
        from wms.api.pickings_listado import avance_programacion
        if not obj.programacion_destino:
            return None
        return avance_programacion(obj, self._ctx_fila(obj)["surtido"].get(obj.pedido_id, {}))

    def get_pedido_fecha_entrega(self, obj):
        from wms.api.pickings_listado import fecha_entrega
        return fecha_entrega(obj)

    class Meta:
        model = Picking
        fields = "__all__"
        read_only_fields = [
            "folio",
            "transferencia",
            "total_lineas",
            "total_lineas_completas",
            "usuario",
            "created_at",
            "updated_at",
            "empresa",
            "sucursal",
            "motivo_cancelacion", "fecha_cancelacion", "cancelado_por",
        ]

    def get_operador_nombre(self, obj):
        return _nombre_usuario(obj.operador)

    def get_usuario_nombre(self, obj):
        return _nombre_usuario(obj.usuario)

    def get_oleada_nombre(self, obj):
        return str(obj.oleada) if obj.oleada_id else None

    def get_pedido_vinculado(self, obj):
        return armar_pedido_vinculado(obj)

    def get_cajas(self, obj):
        return _cajas_de_packing(obj)

    def get_lote_nombre(self, obj):
        return str(obj.lote) if obj.lote_id else None



class PickingListSerializer(PickingSerializer):
    """Listado ligero (``?resumen=1``, #1149): el encabezado sin ``picking_detalle``
    ni ``ordenes_trabajo``. Mismos nombres resueltos que el detalle."""

    picking_detalle = None
    ordenes_trabajo = None
    # Totales que ``pickings_listado.anotar`` calcula en la misma consulta del listado.
    piezas = serializers.DecimalField(max_digits=18, decimal_places=4, read_only=True, coerce_to_string=False)
    productos = serializers.IntegerField(read_only=True)
    tallas = serializers.IntegerField(read_only=True)

class PickingTrasladoSerializer(serializers.Serializer):
    """Entrada de ``POST /pickings/{id}/trasladar/``: almacén al que se mueve."""

    almacen = serializers.PrimaryKeyRelatedField(queryset=Almacen.objects.all())
    observaciones = serializers.CharField(required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Un almacén de otra empresa responde igual que uno inexistente (no revela IDs).
        # Superusuario incluido: se acota a su empresa activa (EC-798, #1154).
        user = getattr(self.context.get("request"), "user", None)
        if user is not None:
            empresa_id = getattr(user, "empresa_id", None)
            self.fields["almacen"].queryset = Almacen.objects.filter(empresa_id=empresa_id)


class PickingCreateSerializer(serializers.ModelSerializer):
    """Alta de picking parcial.

    El frontend envía encabezado + las cantidades reales por línea/talla que se
    surtirán en este picking. El backend valida contra el pendiente calculado a
    partir del historial de ``PickingDetalle``.
    """

    class PickingCreateDetalleInputSerializer(serializers.Serializer):
        pedido_detalle_talla = serializers.IntegerField(min_value=1)
        cantidad_asignada = serializers.DecimalField(
            max_digits=18,
            decimal_places=4,
            min_value=Decimal("0.0001"),
        )
        observaciones = serializers.CharField(required=False, allow_blank=True)
        generar_orden_bordado = serializers.BooleanField(required=False, default=False)
        generar_orden_reflejante = serializers.BooleanField(required=False, default=False)
        generar_orden_corte_manga = serializers.BooleanField(required=False, default=False)

    picking_detalle = PickingCreateDetalleInputSerializer(many=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Mismo criterio que ``PickingTrasladoSerializer``: un id de otra empresa
        # responde igual que uno inexistente (#740).
        # Superusuario incluido: se acota a su empresa activa (EC-798, #1154).
        user = getattr(self.context.get("request"), "user", None)
        if user is not None:
            empresa_id = getattr(user, "empresa_id", None)
            for campo in ("pedido", "operador", "almacen", "almacen_destino"):
                qs = self.fields[campo].queryset
                self.fields[campo].queryset = qs.filter(empresa_id=empresa_id)
            # Oleada, zona y lote no tienen ``empresa``: se llega por su dueño.
            for campo, ruta in (
                ("zona_almacen", "almacen__empresa_id"),
                ("lote", "almacen__empresa_id"),
                ("oleada", "usuario__empresa_id"),
            ):
                qs = self.fields[campo].queryset
                self.fields[campo].queryset = qs.filter(**{ruta: empresa_id})

    class Meta:
        model = Picking
        fields = [
            "pedido",
            "operador",
            "almacen",
            "almacen_destino",
            "programacion_destino",
            "oleada",
            "zona_almacen",
            "lote",
            "prioridad",
            "tipo",
            "fecha_inicio",
            "fecha_fin",
            "fecha_limite",
            "observaciones",
            "picking_detalle",
        ]

class PackingDetalleReadSerializer(_VarianteDeLineaWmsMixin, serializers.ModelSerializer):
    """Renglón anidado de un packing: los datos del producto salen de su ``picking_detalle``.

    Línea de muestra (EC-882): su ``picking_detalle`` no tiene ``producto`` ni
    ``producto_variante`` sino ``variante_produccion``. ``producto_nombre`` cae al
    nombre de esa variante y ``talla_*`` a su talla —las mismas reglas que
    ``PickingDetalleSerializer``—, ``sku_muestra`` lleva su SKU y ``sku``/``color_*``
    salen de la variante de catálogo o de la de muestra. ``talla_id`` ya no se
    omite cuando falta la cadena de la talla del pedido: viaja en ``null``.
    """

    producto = serializers.IntegerField(
        source="picking_detalle.producto_id", read_only=True
    )
    producto_variante = serializers.IntegerField(
        source="picking_detalle.producto_variante_id", read_only=True
    )
    producto_variante_nombre = serializers.CharField(
        source="picking_detalle.producto_variante.nombre", read_only=True, default=None
    )
    variante_produccion = serializers.IntegerField(
        source="picking_detalle.variante_produccion_id", read_only=True
    )
    sku_muestra = serializers.CharField(
        source="picking_detalle.variante_produccion.sku", read_only=True, default=None
    )
    pedido_detalle = serializers.IntegerField(
        source="picking_detalle.pedido_detalle_id", read_only=True
    )
    pedido_detalle_talla = serializers.IntegerField(
        source="picking_detalle.pedido_detalle_talla_id", read_only=True
    )
    cantidad_asignada = serializers.DecimalField(
        source="picking_detalle.cantidad_asignada",
        max_digits=18,
        decimal_places=4,
        read_only=True,
    )
    cantidad_solicitada = serializers.DecimalField(
        source="picking_detalle.cantidad_solicitada",
        max_digits=18,
        decimal_places=4,
        read_only=True,
    )
    cantidad_surtida = serializers.DecimalField(
        source="picking_detalle.cantidad_surtida",
        max_digits=18,
        decimal_places=4,
        read_only=True,
    )
    ubicacion = serializers.IntegerField(
        source="picking_detalle.ubicacion_id", read_only=True
    )
    ubicacion_nombre = serializers.SerializerMethodField()
    caja_numero = serializers.IntegerField(source="caja.numero", read_only=True, default=None)

    class Meta:
        model = PackingDetalle
        fields = "__all__"
        read_only_fields = ["packing"]

    def get_ubicacion_nombre(self, obj):
        return str(obj.picking_detalle.ubicacion) if obj.picking_detalle.ubicacion_id else None

    def _linea_wms(self, obj):
        return obj.picking_detalle

class PackingCajaContenidoSerializer(_VarianteDeLineaWmsMixin, serializers.Serializer):
    """Renglón de ``packing.cajas[].contenido``: un ``packing_detalle`` de esa caja."""

    picking_detalle = serializers.IntegerField(source="picking_detalle_id", read_only=True)
    cantidad = serializers.DecimalField(
        source="cantidad_empacada", max_digits=18, decimal_places=4, read_only=True
    )

    def _linea_wms(self, obj):
        return obj.picking_detalle


def _cajas_de_packing(packing):
    """``cajas`` del packing armadas en memoria desde ``packing_cajas`` y
    ``packing_detalle`` precargados: sin consultas por caja ni por renglón."""
    por_caja = {}
    for det in packing.packing_detalle.all():
        if det.caja_id:
            por_caja.setdefault(det.caja_id, []).append(det)
    salida = []
    for caja in sorted(packing.packing_cajas.all(), key=lambda c: c.numero):
        renglones = por_caja.get(caja.pk, [])
        salida.append({
            "id": caja.pk,
            "numero": caja.numero,
            "peso": str(caja.peso),
            "largo": None if caja.largo is None else str(caja.largo),
            "ancho": None if caja.ancho is None else str(caja.ancho),
            "alto": None if caja.alto is None else str(caja.alto),
            "volumen": None if caja.volumen is None else str(caja.volumen),
            "piezas": str(normalizar_decimal(sum((r.cantidad_empacada for r in renglones), Decimal("0")))),
            "contenido": [
                {k: v for k, v in fila.items() if k not in ("color_id", "talla_id")}
                for fila in PackingCajaContenidoSerializer(renglones, many=True).data
            ],
        })
    return salida


class PackingSerializer(_CanceladoPorNombreMixin, serializers.ModelSerializer):
    """Serializer compartido de packing (``list``, ``retrieve`` y respuesta del alta).

    ``pedido_vinculado`` (EC-882): ``{id, folio, cliente}`` del pedido, misma forma
    que en picking y en los detalles de Producción (``armar_pedido_vinculado``).
    """

    packing_detalle = PackingDetalleReadSerializer(many=True, read_only=True)
    cajas = serializers.SerializerMethodField()
    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True)
    pedido_vinculado = serializers.SerializerMethodField()
    picking_folio = serializers.CharField(source="picking.folio", read_only=True)
    picking_estado = serializers.CharField(source="picking.estado", read_only=True)
    picking_almacen = serializers.IntegerField(source="picking.almacen_id", read_only=True)
    picking_almacen_nombre = serializers.CharField(
        source="picking.almacen.nombre", read_only=True, default=None
    )
    operador_nombre = serializers.SerializerMethodField()
    usuario_nombre = serializers.SerializerMethodField()

    class Meta:
        model = Packing
        fields = "__all__"
        read_only_fields = [
            "folio", 
            "empresa", 
            "sucursal", 
            "pedido", 
            "operador", 
            "usuario", 
            "created_at", 
            "updated_at",
            "motivo_cancelacion", "fecha_cancelacion", "cancelado_por",
        ]

        
    def get_operador_nombre(self, obj):
        return _nombre_usuario(obj.operador)
    
    def get_pedido_vinculado(self, obj):
        return armar_pedido_vinculado(obj)

    def get_usuario_nombre(self, obj):
        return _nombre_usuario(obj.usuario)

    def get_cajas(self, obj):
        return _cajas_de_packing(obj)


class PackingCreateSerializer(serializers.ModelSerializer):
    """Alta de packing guiada por picking.

    El frontend envía el ``picking`` origen y las líneas concretas que va a
    empacar. El backend valida contra lo ya registrado históricamente para
    evitar sobre-empaque.
    """

    class PackingCreateDetalleInputSerializer(serializers.Serializer):
        picking_detalle = serializers.IntegerField(min_value=1)
        cantidad_empacada = serializers.DecimalField(
            max_digits=18,
            decimal_places=4,
            min_value=Decimal("0.0001"),
        )
        observaciones = serializers.CharField(required=False, allow_blank=True)

    class PackingCreateCajaInputSerializer(serializers.Serializer):
        class PackingCreateContenidoInputSerializer(serializers.Serializer):
            picking_detalle = serializers.IntegerField(min_value=1)
            cantidad = serializers.DecimalField(
                max_digits=18, decimal_places=4, min_value=Decimal("0.0001")
            )

        numero = serializers.IntegerField(min_value=1)
        peso = serializers.DecimalField(
            max_digits=18, decimal_places=3, min_value=Decimal("0.001"),
            error_messages={"min_value": "El peso de la caja debe ser mayor a 0."},
        )
        largo = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True)
        ancho = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True)
        alto = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0"), required=False, allow_null=True)
        observaciones = serializers.CharField(required=False, allow_blank=True, allow_null=True)
        contenido = PackingCreateContenidoInputSerializer(
            many=True, allow_empty=False,
            error_messages={"empty": "La caja debe llevar al menos una pieza."},
        )

    packing_detalle = PackingCreateDetalleInputSerializer(many=True, required=False)
    cajas = PackingCreateCajaInputSerializer(many=True, required=False, allow_empty=False)

    def validate(self, attrs):
        if attrs.get("cajas") and attrs.get("packing_detalle"):
            raise serializers.ValidationError(
                {"cajas": "Envía cajas o packing_detalle, no ambos."}
            )
        if not attrs.get("cajas") and not attrs.get("packing_detalle"):
            raise serializers.ValidationError(
                {"cajas": "Debe enviar al menos una caja con contenido."}
            )
        cajas = attrs.get("cajas") or []
        numeros = [c["numero"] for c in cajas]
        if cajas and sorted(numeros) != list(range(1, len(cajas) + 1)):
            raise serializers.ValidationError(
                {"cajas": "Los números de caja deben ser consecutivos desde 1 y sin repetirse."}
            )
        return attrs

    class Meta:
        model = Packing
        fields = [
            "picking",
            "numero_cajas",
            "peso_total",
            "volumen_total",
            "fecha_inicio",
            "fecha_fin",
            "observaciones",
            "packing_detalle",
            "cajas",
        ]


class DespachoDetalleReadSerializer(serializers.ModelSerializer):
    picking_detalle = serializers.IntegerField(
        source="packing_detalle.picking_detalle_id", read_only=True
    )
    pedido_detalle = serializers.IntegerField(
        source="packing_detalle.picking_detalle.pedido_detalle_id", read_only=True
    )
    pedido_detalle_talla = serializers.IntegerField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla_id", read_only=True
    )
    producto = serializers.IntegerField(
        source="packing_detalle.picking_detalle.producto_id", read_only=True
    )
    producto_nombre = serializers.CharField(
        source="packing_detalle.picking_detalle.producto.nombre", read_only=True, default=None
    )
    producto_variante = serializers.IntegerField(
        source="packing_detalle.picking_detalle.producto_variante_id", read_only=True
    )
    producto_variante_nombre = serializers.CharField(
        source="packing_detalle.picking_detalle.producto_variante.nombre",
        read_only=True,
        default=None,
    )
    talla_id = serializers.IntegerField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla.variante.talla_id",
        read_only=True,
    )
    talla_nombre = serializers.CharField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla.variante.talla.nombre",
        read_only=True,
        default=None,
    )
    color_id = serializers.IntegerField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla.variante.color_id",
        read_only=True,
    )
    color_nombre = serializers.CharField(
        source="packing_detalle.picking_detalle.pedido_detalle_talla.variante.color.nombre",
        read_only=True,
        default=None,
    )
    ubicacion = serializers.IntegerField(
        source="packing_detalle.picking_detalle.ubicacion_id", read_only=True
    )
    ubicacion_nombre = serializers.SerializerMethodField()
    caja = serializers.IntegerField(source="packing_detalle.caja_id", read_only=True)
    caja_numero = serializers.IntegerField(
        source="packing_detalle.caja.numero", read_only=True, default=None
    )
    cantidad_empacada = serializers.DecimalField(
        source="packing_detalle.cantidad_empacada",
        max_digits=18,
        decimal_places=4,
        read_only=True,
    )
    estado = serializers.CharField(source="packing_detalle.estado", read_only=True)

    class Meta:
        model = DespachoDetalle
        fields = "__all__"
        read_only_fields = ["despacho"]

    def get_ubicacion_nombre(self, obj):
        ubicacion = getattr(obj.packing_detalle.picking_detalle, "ubicacion", None)
        return str(ubicacion) if ubicacion else None


class DespachoSerializer(_CanceladoPorNombreMixin, serializers.ModelSerializer):
    despacho_detalle = DespachoDetalleReadSerializer(many=True, read_only=True)
    packing_folio = serializers.CharField(source="packing.folio", read_only=True)
    packing_estado = serializers.CharField(source="packing.estado", read_only=True)
    pedido = serializers.IntegerField(source="packing.pedido_id", read_only=True)
    pedido_folio = serializers.CharField(source="packing.pedido.folio", read_only=True)
    cliente = serializers.IntegerField(source="packing.pedido.cliente_id", read_only=True)
    cliente_nombre = serializers.CharField(
        source="packing.pedido.cliente.nombre", read_only=True, default=None
    )
    sucursal = serializers.IntegerField(source="packing.sucursal_id", read_only=True)
    sucursal_nombre = serializers.CharField(
        source="packing.sucursal.nombre", read_only=True, default=None
    )
    envio_transportista = serializers.IntegerField(
        source="envio.transportista_id", read_only=True, default=None
    )
    envio_transportista_nombre = serializers.CharField(
        source="envio.transportista.nombre", read_only=True, default=None
    )
    tipo_envio_display = serializers.CharField(source="get_tipo_envio_display", read_only=True, default=None)
    paqueteria_display = serializers.CharField(source="get_paqueteria_display", read_only=True, default=None)
    chofer_nombre = serializers.SerializerMethodField()
    fecha_creacion = serializers.DateTimeField(source="created_at", read_only=True, allow_null=True)
    usuario_nombre = serializers.SerializerMethodField()
    # EC-979: quién registró la entrega, misma regla que ``cancelado_por_nombre``;
    # ``null`` si no está entregado. ``entregado_por`` viaja en el ``select_related``.
    entregado_por_nombre = serializers.SerializerMethodField()

    def get_usuario_nombre(self, obj):
        return _nombre_usuario(obj.usuario) if obj.usuario_id else None

    @extend_schema_field({"type": "string", "nullable": True})
    def get_entregado_por_nombre(self, obj):
        return _nombre_usuario(obj.entregado_por) if obj.entregado_por_id else None

    def get_chofer_nombre(self, obj):
        return nombre_chofer(obj.chofer) if obj.chofer_id else None

    class Meta:
        model = Despacho
        fields = "__all__"


class DespachoCreateSerializer(serializers.ModelSerializer):
    class DespachoCreateDetalleInputSerializer(serializers.Serializer):
        packing_detalle = serializers.IntegerField(min_value=1)

    envio = serializers.PrimaryKeyRelatedField(
        queryset=Envio.objects.all(),
        required=False,
        allow_null=True,
    )
    despacho_detalle = DespachoCreateDetalleInputSerializer(many=True)
    tipo_envio = serializers.ChoiceField(choices=Despacho.TipoEnvio.choices)
    paqueteria = serializers.ChoiceField(
        choices=Despacho.Paqueteria.choices, required=False, allow_blank=True
    )
    chofer = serializers.PrimaryKeyRelatedField(
        queryset=Empleado.objects.filter(es_chofer=True, activo=True),
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        tipo = attrs["tipo_envio"]
        errores = {}
        if tipo == Despacho.TipoEnvio.PAQUETERIA:
            if not attrs.get("paqueteria"):
                errores["paqueteria"] = "Obligatoria si el envío es por paquetería."
            if not (attrs.get("guia") or "").strip():
                errores["guia"] = "Obligatoria si el envío es por paquetería."
            attrs["chofer"] = None
        else:
            chofer = attrs.get("chofer")
            user = getattr(self.context.get("request"), "user", None)
            if chofer is None:
                errores["chofer"] = "Obligatorio si el envío es local."
            elif user is not None and chofer.empresa_id != getattr(user, "empresa_id", None):
                errores["chofer"] = "El chofer no pertenece a tu empresa."
            attrs["paqueteria"] = ""
        if errores:
            raise serializers.ValidationError(errores)
        return attrs

    class Meta:
        model = Despacho
        fields = [
            "packing",
            "envio",
            "guia",
            "tipo_envio",
            "paqueteria",
            "chofer",
            "despacho_detalle",
        ]


def nombre_chofer(empleado):
    return " ".join(x for x in (empleado.nombre, empleado.apellido_paterno, empleado.apellido_materno) if x)


class EtiquetaRFIDDetalleReadSerializer(serializers.ModelSerializer):
    class Meta:
        model = EtiquetaRFIDDetalle
        fields = "__all__"
        read_only_fields = ["impresion"]


class EtiquetaRFIDSerializer(serializers.ModelSerializer):
    folio = serializers.CharField(read_only=True)
    etiquetas = EtiquetaRFIDDetalleReadSerializer(many=True, read_only=True)
    empresa = serializers.IntegerField(source="empresa_id", read_only=True)
    sucursal = serializers.IntegerField(source="sucursal_id", read_only=True, allow_null=True)
    usuario = serializers.IntegerField(source="usuario_id", read_only=True, allow_null=True)
    producto = serializers.IntegerField(source="producto_id", read_only=True, allow_null=True)
    producto_variante = serializers.IntegerField(
        source="producto_variante_id", read_only=True, allow_null=True
    )
    producto_nombre = serializers.CharField(
        source="producto.nombre", read_only=True, default=None
    )
    producto_variante_nombre = serializers.CharField(
        source="producto_variante.nombre", read_only=True, default=None
    )
    sku = serializers.CharField(
        source="producto_variante.sku", read_only=True, default=None
    )
    codigo_producto = serializers.CharField(
        source="producto.codigo", read_only=True, default=None
    )

    class Meta:
        model = EtiquetaRFIDImpresion
        fields = "__all__"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # El frontend reimprime ``zpl_enviado`` desde el historial: las
        # impresiones viejas traen un bloque RFID que no graba el chip.
        if instance.rfid_mode:
            data["zpl_enviado"] = zpl_rfid_vigente(data.get("zpl_enviado"))
        return data


class EtiquetaRFIDCreateSerializer(serializers.Serializer):
    class EtiquetaRFIDDetalleInputSerializer(serializers.Serializer):
        epc = serializers.CharField(max_length=64, required=False, allow_blank=True)
        barcode_value = serializers.CharField(
            max_length=128, required=False, allow_blank=True
        )
        serial = serializers.CharField(
            max_length=64, required=False, allow_blank=True, allow_null=True
        )

    producto_variante = serializers.PrimaryKeyRelatedField(
        queryset=ProductoVariante.objects.all(),
        required=False,
        allow_null=True,
    )
    producto = serializers.PrimaryKeyRelatedField(
        queryset=Producto.objects.all(),
        required=False,
        allow_null=True,
    )
    cantidad = serializers.IntegerField(min_value=1, max_value=10000, required=False, default=1)
    rfid_mode = serializers.BooleanField(required=False, default=True)
    printer_name = serializers.CharField(
        max_length=128, required=False, allow_blank=True, allow_null=True
    )
    printer_address = serializers.CharField(
        max_length=128, required=False, allow_blank=True, allow_null=True
    )
    status = serializers.ChoiceField(
        choices=EtiquetaRFIDImpresion.Estatus.choices, required=False
    )
    zpl_enviado = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    observaciones = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    etiquetas = EtiquetaRFIDDetalleInputSerializer(many=True, required=False)

    def validate(self, attrs):
        producto = attrs.get("producto")
        producto_variante = attrs.get("producto_variante")
        if not producto and not producto_variante:
            raise serializers.ValidationError(
                "Debe proporcionar 'producto' o 'producto_variante'."
            )
        if producto and producto_variante:
            raise serializers.ValidationError(
                "Solo puede proporcionar 'producto' o 'producto_variante'."
            )

        # Pre-chequeo de unicidad de EPC antes de tocar la BD. ``EtiquetaRFIDDetalle
        # .epc`` es ``unique=True`` y el ``bulk_create`` del service no puede
        # devolver un mensaje útil (Postgres aborta en la primera fila en conflicto),
        # así que se rechaza aquí con 400 y cuerpo por campo —mismo estilo que el
        # pre-chequeo de RFC duplicado en ``terceros.api.serializers``—.
        #
        # Se normaliza con ``.strip().upper()`` porque el service guarda
        # ``epc.upper()``: sin normalizar, un reenvío en minúsculas se saltaría este
        # filtro y terminaría chocando en el índice único de todas formas.
        # Sólo aplica con ``rfid_mode`` activo: con él apagado ``store_impresion``
        # se salta por completo la creación de ``EtiquetaRFIDDetalle``, así que no
        # hay unicidad que violar. Sin esta condición, reimprimir etiquetas
        # visuales de tags ya registrados —el preview devuelve ``etiquetas``
        # siempre, sin importar ``rfid_mode``, y el front reenvía ese payload— se
        # rechazaba con 400 por un conflicto que nunca habría ocurrido.
        epcs = [
            (item.get("epc") or "").strip().upper()
            for item in (attrs.get("etiquetas") or [])
        ]
        epcs = [epc for epc in epcs if epc]
        if epcs and attrs.get("rfid_mode", True):
            if len(set(epcs)) != len(epcs):
                raise serializers.ValidationError(
                    {"etiquetas": "El arreglo enviado trae códigos EPC repetidos."}
                )
            # Sin filtrar por empresa: la constraint es global. Y el mensaje se
            # mantiene genérico a propósito —no dice cuál EPC ni de qué empresa—
            # porque delatar que un EPC existe en otro tenant es fuga cross-tenant.
            if EtiquetaRFIDDetalle.objects.filter(epc__in=epcs).exists():
                raise serializers.ValidationError(
                    {"etiquetas": "Uno o más códigos EPC ya están registrados."}
                )

        return attrs


