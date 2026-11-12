from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from ventas.models import Pedido, PedidoDetalle, PedidoDetalleTalla

from produccion.models import (
    PreOrdenEvento,
    PreOrdenProduccion,
    PreOrdenProduccionDetalle,
    ListaMaterialBom,
    BomDetalle,
    OrdenProduccion,
    OrdenProduccionDetalle,
    OrdenProduccionRutaCritica,
    ConsumoProduccion,
    ConsumoProduccionDetalle,
    ProductoTerminadoEntradas, 
    OrdenesBordado,
    OrdenBordadoDetalle,
    BordadoAvances,
    BordadoIncidencias,
    OrdenesReflejante,
    OrdenReflejanteDetalle,
    ReflejanteAvances,
    ReflejanteIncidencias,
    OrdenesCorteManga,
    OrdenCorteMangaDetalle,
    CorteMangaAvances,
)

from catalogo.api.serializers import ProductoVarianteSerializer
from catalogo.models import Color, ProductoVariante, VarianteProductoProduccion
from inventarios.models import Ubicacion
from produccion.services.common import config_como_dict, revisar_empresa


class _SkuPorTallaMixin:
    """``sku`` de la variante (producto+color+talla) del renglón, o ``null``.

    El ``retrieve`` de la orden deja ``sku_por_linea`` en el contexto
    (``{(pedido_detalle_id, talla_id): sku}``), armado en la MISMA query que ya
    lee ``PedidoDetalleTalla`` para el config: cero queries extra. Para una
    muestra sin variante de catálogo se publica su ``sku_muestra``.
    """

    def get_sku(self, obj):
        mapa = self.context.get("sku_por_linea") or {}
        return mapa.get((obj.pedido_detalle_id, obj.talla_id)) or None


def leer_mapa_pdt(pedido_id, config_field):
    """``(config_map, sku_map)`` de todas las tallas del pedido en UNA query.

    ``config_map`` es el mismo mapa que ya armaba cada ``retrieve``
    (``pdt_*_config_map``); ``sku_map`` sale de la misma fila (JOIN a la
    variante).
    """
    config_map, sku_map = {}, {}
    for pd_id, talla_id, cfg, sku_var, sku_muestra in (
        PedidoDetalleTalla.objects
        .filter(pedido_detalle__pedido_id=pedido_id)
        .order_by("id")
        .values_list("pedido_detalle_id", "talla_id", config_field, "variante__sku", "sku_muestra")
    ):
        # ``setdefault``: si hubiera dos filas para el mismo par se conserva la
        # primera, como hacía ``.first()``.
        config_map.setdefault((pd_id, talla_id), cfg)
        sku_map.setdefault((pd_id, talla_id), sku_var or sku_muestra or None)
    return config_map, sku_map


class AvancePiezasReadSerializer(serializers.Serializer):
    """Avance de piezas (reflejante / corte de manga) en el retrieve de la orden."""

    id = serializers.IntegerField(read_only=True)
    fecha = serializers.DateTimeField(read_only=True)
    cantidad = serializers.FloatField(read_only=True)
    comentario = serializers.CharField(read_only=True, allow_null=True)
    usuario = serializers.IntegerField(source="usuario_id", read_only=True)
    usuario_nombre = serializers.SerializerMethodField()

    def get_usuario_nombre(self, obj):
        usuario = getattr(obj, "usuario", None)
        if not usuario:
            return None
        return usuario.get_full_name().strip() or getattr(usuario, "email", None)


def resumen_avance_piezas(detalles, avances):
    """``{cantidad_programada, cantidad_realizada_total, porcentaje_avance}``.

    Mismo cálculo de piezas que ``OrdenBordadoRetrieveSerializer.get_resumen_avance``
    (sin puntadas), sobre lo ya en memoria: renglones prefetcheados y avances
    del contexto.
    """
    programada = sum((float(d.cantidad or 0) for d in detalles), 0.0)
    realizada = sum((float(a.cantidad or 0) for a in avances), 0.0)
    pct = round(realizada / programada * 100.0, 2) if programada > 0 else 0.0
    return {
        "cantidad_programada": programada,
        "cantidad_realizada_total": realizada,
        "porcentaje_avance": pct,
    }


class _AvancePiezasWriteSerializer(serializers.ModelSerializer):
    """Alta de avance de piezas: ``{<orden>, cantidad, comentario?}``.

    El usuario sale de la sesión (``perform_create``). La orden debe estar en
    el alcance del solicitante (``_OrdenPadreWriteOnceMixin``); orden abierta y
    tope de piezas se validan en la vista, con la orden bloqueada.
    """

    usuario_nombre = serializers.SerializerMethodField()

    def get_usuario_nombre(self, obj):
        usuario = getattr(obj, "usuario", None)
        if not usuario:
            return None
        return usuario.get_full_name().strip() or getattr(usuario, "email", None)

    def validate_cantidad(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("La cantidad debe ser mayor a 0.")
        if value != int(value):
            raise serializers.ValidationError("La cantidad debe ser un número entero de piezas.")
        return value


def _pedido_en_alcance(serializer, pedido, etiqueta_orden, destino):
    """``pedido`` de una OB/OR/OCM, validado a nivel de campo.

    A nivel de campo y no solo en el service: si falla, ``validate()`` no corre
    y sus mensajes sobre ``detalles_override`` no exponen tallas de otra
    empresa. Mismo criterio que ``_validar_contexto`` de los services, que se
    queda como segunda puerta.
    """
    if pedido is None:
        return pedido
    if serializer.instance is not None and serializer.instance.pedido_id == pedido.pk:
        return pedido
    user = getattr(serializer.context.get("request"), "user", None)
    resultado = revisar_empresa(user, pedido)
    if resultado == "sin_empresa":
        raise serializers.ValidationError("El usuario no tiene una empresa asignada.")
    if resultado == "otra_empresa":
        # Mismo mensaje que un pk inexistente: no revela que existe en otra empresa.
        raise serializers.ValidationError(
            serializer.fields["pedido"].error_messages["does_not_exist"].format(pk_value=pedido.pk)
        )
    es_staff = getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)
    if not es_staff and pedido.sucursal_id not in user.sucursales_permitidas():
        raise serializers.ValidationError(
            f"No tiene acceso a la sucursal del pedido para generar la orden de {etiqueta_orden}."
        )
    # Mismo criterio que el GET de onboarding: solo pedidos que Mesa de Control
    # programó hacia este destino.
    from produccion.api.views import _programado_para_destino
    if _programado_para_destino(pedido, destino) is None:
        raise serializers.ValidationError(
            f"El pedido no está programado hacia {destino} por Mesa de Control."
        )
    return pedido


#: Centinela de ``_fk_de_la_empresa_del_usuario``: "la empresa es la del propio objeto".
_EL_PROPIO_OBJETO = object()


def _fk_de_la_empresa_del_usuario(serializer, campo, obj, con_empresa=_EL_PROPIO_OBJETO):
    """FK escribible de BOM/OP, acotada a la empresa del usuario autenticado.

    ``con_empresa`` es el objeto que lleva ``empresa_id``: por defecto el propio
    ``obj``; los casos indirectos pasan su relación (``pedido_detalle.pedido``,
    ``ubicacion.almacen``) y, si esa relación viene vacía, se rechaza.

    La comparación es la de ``revisar_empresa``: SIEMPRE contra
    ``user.empresa``, superusuario incluido -- el mismo alcance que el
    ``get_queryset`` de ``lista-material``, ``bom-detalle`` y
    ``orden-produccion``, que tampoco le dan vista global. Sin ``request`` en el
    contexto o sin empresa se rechaza (falla cerrado).
    """
    if obj is None:
        return obj
    if con_empresa is _EL_PROPIO_OBJETO:
        con_empresa = obj
    elif con_empresa is None:
        con_empresa = SimpleNamespace(empresa_id=None)
    user = getattr(serializer.context.get("request"), "user", None)
    resultado = revisar_empresa(user, con_empresa)
    if resultado == "sin_empresa":
        raise serializers.ValidationError("El usuario no tiene una empresa asignada.")
    if resultado == "otra_empresa":
        # Mismo mensaje que un pk inexistente: no revela que existe en otra empresa.
        raise serializers.ValidationError(
            serializer.fields[campo].error_messages["does_not_exist"].format(pk_value=obj.pk)
        )
    return obj


class BomDetalleSerializer(serializers.ModelSerializer):
    componente_nombre = serializers.SerializerMethodField()
    unidad_clave = serializers.SerializerMethodField()

    def get_componente_nombre(self, obj):
        return obj.componente.nombre if obj.componente else None

    def get_unidad_clave(self, obj):
        return obj.unidad.clave if obj.unidad else None

    def validate(self, attrs):
        """La unidad sale del material (``Producto.unidad_medida``): si llega otra,
        error por campo. Si el material aún no tiene unidad, se exige en el
        payload y se le queda al material (las siguientes veces ya no se pide)."""
        attrs = super().validate(attrs)
        componente = attrs.get("componente", getattr(self.instance, "componente", None))
        unidad = attrs.get("unidad")
        if componente is not None and componente.unidad_medida_id:
            if unidad is not None and unidad.pk != componente.unidad_medida_id:
                raise serializers.ValidationError(
                    {"unidad": f"La unidad del material es {componente.unidad_medida.clave}; no se puede cambiar en el BOM."}
                )
            attrs["unidad"] = componente.unidad_medida
        elif unidad is None and self.instance is None:
            raise serializers.ValidationError({"unidad": "El material no tiene unidad; indícala."})
        elif componente is not None and unidad is not None:
            type(componente).objects.filter(pk=componente.pk, unidad_medida__isnull=True).update(unidad_medida=unidad)
            componente.unidad_medida = unidad
        return attrs

    # Aislamiento multi-tenant: aplica a todos los consumidores del serializer
    # (anidado en ``lista-material``, ``bom-detalle`` y ``variante-onboarding``).
    def validate_componente(self, componente):
        return _fk_de_la_empresa_del_usuario(self, 'componente', componente)

    def validate_variante_produccion(self, variante_produccion):
        return _fk_de_la_empresa_del_usuario(self, 'variante_produccion', variante_produccion)

    class Meta:
        model = BomDetalle
        fields = '__all__'
        read_only_fields = ['bom', 'activo']
        extra_kwargs = {'unidad': {'required': False}}


def validar_sin_duplicados(detalles):
    """Un material una sola vez por BOM (y variante): error por renglón."""
    vistos, errores = {}, [{} for _ in detalles]
    for i, d in enumerate(detalles):
        comp = d.get("componente")
        clave = (getattr(comp, "pk", comp), getattr(d.get("variante_produccion"), "pk", None))
        if clave[0] is None:
            continue
        if clave in vistos:
            errores[i] = {"componente": [f"Material repetido (renglón {vistos[clave] + 1}); junta las cantidades en un solo renglón."]}
        else:
            vistos[clave] = i
    if any(errores):
        raise serializers.ValidationError(errores)
    return detalles

class BomBulkItemSerializer(serializers.Serializer):
    producto_variante_id = serializers.IntegerField()
    bom_id = serializers.IntegerField(allow_null=True)
    detalles = BomDetalleSerializer(many=True)

    def validate_detalles(self, value):
        return validar_sin_duplicados(value)

class ListaMaterialBomSerializer(serializers.ModelSerializer):
    materia_prima_detalle = BomDetalleSerializer(many=True)

    def validate_materia_prima_detalle(self, value):
        return validar_sin_duplicados(value)

    class Meta:
        model = ListaMaterialBom
        fields = '__all__'
        # ``empresa`` no se confía al body: en create la inyecta
        # ``ListaMaterialBomViewSet.perform_create`` desde el usuario
        # autenticado y en update no se reasigna. Sigue saliendo en la respuesta.
        read_only_fields = ['activo', 'bom_id', 'empresa']

    def validate_producto_variante(self, producto_variante):
        return _fk_de_la_empresa_del_usuario(self, 'producto_variante', producto_variante)

    def validate_variante_produccion(self, variante_produccion):
        return _fk_de_la_empresa_del_usuario(self, 'variante_produccion', variante_produccion)

    def create(self, validated_data):
        detalles_data = validated_data.pop('materia_prima_detalle')
        producto_variante = validated_data.get('producto_variante')

        try:
            with transaction.atomic():
                # Buscar un BOM existente para la misma variante de producto
                # dentro de la empresa (un BOM por producto_variante).
                bom = None
                if producto_variante is not None:
                    bom = ListaMaterialBom.objects.filter(
                        empresa=validated_data.get('empresa'),
                        producto_variante=producto_variante,
                    ).first()

                if bom is None:
                    # No existe BOM -> crear el BOM y todos sus detalles.
                    bom = ListaMaterialBom.objects.create(**validated_data)
                    detalles = [
                        BomDetalle(bom=bom, **detalle)
                        for detalle in detalles_data
                    ]
                    BomDetalle.objects.bulk_create(detalles)
                else:
                    # Ya existe BOM -> fusionar detalles por (bom, componente).
                    for detalle in detalles_data:
                        existente = bom.materia_prima_detalle.filter(
                            componente=detalle.get('componente')
                        ).first()
                        if existente is not None:
                            # Mismo (bom, componente): acumular la cantidad y
                            # refrescar unidad, desperdicio y obligatorio con
                            # los valores del detalle entrante.
                            existente.cantidad = existente.cantidad + detalle['cantidad']
                            existente.unidad = detalle.get('unidad', existente.unidad)
                            existente.desperdicio = detalle.get('desperdicio', existente.desperdicio)
                            existente.obligatorio = detalle.get('obligatorio', existente.obligatorio)
                            existente.save(update_fields=['cantidad', 'unidad', 'desperdicio', 'obligatorio'])
                        else:
                            # Componente nuevo para este BOM: crear el detalle.
                            BomDetalle.objects.create(bom=bom, **detalle)

            return bom
        except Exception as e:
            raise serializers.ValidationError("Error creating bom")

    def update(self, instance, validated_data):
        detalles_data = validated_data.pop('materia_prima_detalle', None)

        try:
            with transaction.atomic():
                for attr, value in validated_data.items():
                    setattr(instance, attr, value)
                instance.save()

                if detalles_data is not None:
                    instance.materia_prima_detalle.all().delete()
                    BomDetalle.objects.bulk_create(
                        [BomDetalle(bom=instance, **detalle) for detalle in detalles_data]
                    )

            return instance
        except Exception:
            raise serializers.ValidationError("Error updating bom")

class OrdenProduccionDetalleSerializer(serializers.ModelSerializer):
    producto_variante = ProductoVarianteSerializer(read_only=True)
    producto_variante_id = serializers.PrimaryKeyRelatedField(
        queryset=ProductoVariante.objects.all(),
        source='producto_variante',
        write_only=True,
        required=False,
        allow_null=True,
    )
    # Alternativa a ``producto_variante_id`` para muestras sin alta en catálogo
    # (SKU ``MP…``): el BOM se resuelve desde la variante de producción.
    variante_produccion_id = serializers.PrimaryKeyRelatedField(
        queryset=VarianteProductoProduccion.objects.select_related('pedido_detalle'),
        source='variante_produccion',
        required=False,
        allow_null=True,
    )
    variante_produccion_sku = serializers.CharField(
        source='variante_produccion.sku', read_only=True, allow_null=True, default=None
    )
    bom_detalle = BomDetalleSerializer(
        source='bom.materia_prima_detalle',
        many=True,
        read_only=True
    )

    # Aislamiento multi-tenant, explícito por campo: antes ``producto_variante``
    # sólo quedaba acotado de rebote por el lookup de BOM de la vista, y
    # ``pedido_detalle`` no se validaba. Que el renglón sea del ``pedido`` de la
    # OP lo revisa ``OrdenProduccionSerializer.validate`` (necesita el encabezado).
    def validate_producto_variante_id(self, producto_variante):
        return _fk_de_la_empresa_del_usuario(self, 'producto_variante_id', producto_variante)

    def validate_variante_produccion_id(self, variante_produccion):
        if variante_produccion is not None and not variante_produccion.activo:
            raise serializers.ValidationError('La variante de producción está inactiva.')
        return _fk_de_la_empresa_del_usuario(self, 'variante_produccion_id', variante_produccion)

    def validate(self, attrs):
        variante_produccion = attrs.get('variante_produccion')
        if self.instance is None or 'producto_variante' in attrs or 'variante_produccion' in attrs:
            if bool(attrs.get('producto_variante')) == bool(variante_produccion):
                raise serializers.ValidationError(
                    'Cada renglón lleva producto_variante_id o variante_produccion_id (uno de los dos).'
                )
        if variante_produccion is not None and variante_produccion.pedido_detalle_id:
            # La variante nace de una línea de muestra: el renglón queda ligado a
            # ella (y ``OrdenProduccionSerializer.validate`` exige que sea del
            # pedido de la OP).
            pedido_detalle = attrs.get('pedido_detalle')
            if pedido_detalle is None:
                attrs['pedido_detalle'] = variante_produccion.pedido_detalle
            elif pedido_detalle.pk != variante_produccion.pedido_detalle_id:
                raise serializers.ValidationError(
                    {'pedido_detalle': 'No corresponde a la línea de muestra de la variante de producción.'}
                )
        return attrs

    def validate_pedido_detalle(self, pedido_detalle):
        return _fk_de_la_empresa_del_usuario(
            self, 'pedido_detalle', pedido_detalle, getattr(pedido_detalle, 'pedido', None)
        )

    class Meta:
        model = OrdenProduccionDetalle
        fields = '__all__'
        # 'bom' ya no es parte del contrato del cliente: se resuelve en el
        # servidor a partir del BOM activo de cada producto_variante.
        read_only_fields = ['activo', 'op', 'bom']
        # ``select_related``: ``validate_pedido_detalle`` lee la empresa del
        # pedido; sin esto era una consulta extra a ``pedidos`` por renglón.
        extra_kwargs = {
            'pedido_detalle': {'queryset': PedidoDetalle.objects.select_related('pedido')},
        }

def estado_ruta_critica(ruta):
    """Semáforo de solo lectura de la ruta crítica de una OP.

    Cada etapa: ``pendiente`` | ``en_proceso`` | ``completo``. Sin captura
    (``ruta`` ``None``) todo está pendiente.
    - desarrollo: completo con ``fecha_liberacion_paquete_tecnico`` (el estatus
      del paquete no tiene valor "liberado"); en proceso si ya tiene estatus.
    - telas_avios: completo con ``kit_completo``; en proceso con existencia o
      surtido de tela o avíos.
    - trazo: completo con ``fecha_trazo``.
    - corte: completo con ``fecha_real_corte`` y ``cantidad_real_corte``; en
      proceso con solo una de las dos.
    - produccion: completo con ``corte_recibido``; en proceso si ya se envió o
      llegó la materia prima al centro de confección.
    """
    P, E, C = "pendiente", "en_proceso", "completo"
    if ruta is None:
        return dict.fromkeys(("desarrollo", "telas_avios", "trazo", "corte", "produccion"), P)

    def etapa(completo, en_proceso=False):
        return C if completo else (E if en_proceso else P)

    return {
        "desarrollo": etapa(ruta.fecha_liberacion_paquete_tecnico, ruta.estatus_paquete_tecnico),
        "telas_avios": etapa(
            ruta.kit_completo,
            ruta.existencia_tela or ruta.existencia_avios
            or ruta.fecha_real_surtido_telas or ruta.fecha_real_surtido_avios,
        ),
        "trazo": etapa(ruta.fecha_trazo),
        "corte": etapa(
            ruta.fecha_real_corte and ruta.cantidad_real_corte,
            ruta.fecha_real_corte or ruta.cantidad_real_corte,
        ),
        "produccion": etapa(
            ruta.corte_recibido,
            ruta.fecha_embarque_materia_prima or ruta.fecha_llegada_centro_confeccion,
        ),
    }


class OrdenProduccionListSerializer(serializers.ModelSerializer):
    """Serializer minimalista para el LISTADO de OP.

    Solo campos escalares que consume la tabla; sin ``orden_produccion_detalle``
    (el nested pesado) para evitar el N+1. Campos explícitos, no ``__all__``.
    """
    estatus_op_display = serializers.CharField(source="get_estatus_op_display", read_only=True)
    # ``select_related("pedido", "centro_confeccion", "ruta_critica")`` y
    # ``cantidad_total`` anotada en el listado: mismo query, sin viajes extra.
    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True, allow_null=True, default=None)
    centro_confeccion = serializers.SerializerMethodField()
    fecha_pedido = serializers.SerializerMethodField()
    fecha_vence = serializers.SerializerMethodField()
    cantidad_total = serializers.FloatField(read_only=True, default=0.0)
    ruta_critica = serializers.SerializerMethodField()
    pedidos = serializers.SerializerMethodField()

    def get_pedidos(self, obj):
        return pedidos_de_op(obj)

    class Meta:
        model = OrdenProduccion
        fields = [
            "op_id", "folio_op", "estatus_op", "estatus_op_display",
            "prioridad", "fecha_inicio", "fecha_fin", "fecha_entrega_estimada", "activo",
            "pedido", "pedido_folio", "pedidos", "sucursal",
            "centro_confeccion", "fecha_pedido", "fecha_vence", "cantidad_total", "ruta_critica",
        ]

    def get_centro_confeccion(self, obj):
        p = obj.centro_confeccion
        return {"id": p.pk, "nombre": p.nombre} if p else None

    def get_fecha_pedido(self, obj):
        from django.utils import timezone
        pedido = obj.pedido
        return timezone.localdate(pedido.created_at) if pedido and pedido.created_at else None

    def get_fecha_vence(self, obj):
        # La misma ``fecha_compromiso`` del listado de pedidos especiales.
        from ventas.services.clasificacion_service import rango_fecha_entrega
        return rango_fecha_entrega(obj.pedido)[1] if obj.pedido else None

    def get_ruta_critica(self, obj):
        return estado_ruta_critica(getattr(obj, "ruta_critica", None))

def pedidos_de_op(op):
    """``[{id, folio}]`` de los pedidos de la OP, principal primero. Lee la
    precarga ``op_pedidos__pedido`` si existe (listado sin consultas por fila)."""
    filas = sorted(op.op_pedidos.all(), key=lambda r: (r.pedido_id != op.pedido_id, r.pk))
    return [{"id": r.pedido_id, "folio": r.pedido.folio} for r in filas]


def ligar_pedidos_op(op, pedidos=None):
    """Liga la OP a su pedido principal y, si se pasan, deja exactamente ``pedidos``
    (+ principal). Con ``pedidos=None`` solo asegura el principal."""
    from produccion.models import OrdenProduccionPedido
    existentes = set(OrdenProduccionPedido.objects.filter(op=op).values_list('pedido_id', flat=True))
    ids = set(existentes) if pedidos is None else {p.pk for p in pedidos}
    if op.pedido_id:
        ids.add(op.pedido_id)
    sobran = existentes - ids
    if sobran:
        OrdenProduccionPedido.objects.filter(op=op, pedido_id__in=sobran).delete()
    nuevos = ids - existentes
    if nuevos:
        OrdenProduccionPedido.objects.bulk_create([OrdenProduccionPedido(op=op, pedido_id=i) for i in nuevos])


class OrdenProduccionSerializer(serializers.ModelSerializer):
    orden_produccion_detalle = OrdenProduccionDetalleSerializer(many=True)
    estatus_op_display = serializers.CharField(source="get_estatus_op_display", read_only=True)
    pedido_vinculado = serializers.SerializerMethodField()
    # Etiquetas legibles junto a las FKs (mismo patrón ``source=`` que
    # ``OrdenBordadoSerializer``/``OrdenReflejanteSerializer``). ``Empresa`` no
    # tiene ``nombre``: su nombre humano es ``razon_social``. ``pedido`` es
    # nullable en OP (a diferencia de OB/OR/OCM), así que ``pedido_folio`` lleva
    # ``allow_null=True`` para devolver ``null`` en vez de 500 cuando no hay
    # pedido.
    empresa_nombre = serializers.CharField(source="empresa.razon_social", read_only=True)
    sucursal_nombre = serializers.CharField(source="sucursal.nombre", read_only=True)
    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True, allow_null=True)
    usuario_nombre = serializers.SerializerMethodField()

    def get_pedido_vinculado(self, obj):
        from produccion.services.orden_bordado_field_filter_service import armar_pedido_vinculado
        return armar_pedido_vinculado(obj)

    def get_usuario_nombre(self, obj):
        usuario = obj.usuario_asignado
        if not usuario: return None
        return usuario.get_full_name().strip() or usuario.email

    # Varios pedidos en una OP: ids de entrada; ``pedidos`` es la lectura.
    pedidos_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), write_only=True, required=False, allow_empty=True,
    )
    pedidos = serializers.SerializerMethodField()

    def get_pedidos(self, obj):
        return pedidos_de_op(obj)

    def validate_pedidos_ids(self, ids):
        ids = list(dict.fromkeys(ids))
        request = self.context.get('request')
        empresa_id = getattr(getattr(request, 'user', None), 'empresa_id', None)
        encontrados = {p.pk: p for p in Pedido.objects.filter(pk__in=ids, empresa_id=empresa_id)}
        ligados = (
            set(self.instance.op_pedidos.values_list('pedido_id', flat=True)) if self.instance is not None else set()
        )
        pedidos = []
        for pk in ids:
            if pk not in encontrados:
                raise serializers.ValidationError(f'Pedido {pk} no encontrado.')
            pedidos.append(self._validar_pedido(encontrados[pk], ya_ligado=pk in ligados))
        return pedidos

    def validate_pedido(self, pedido):
        # Reglas de ``pedido`` aquí y no en la vista: aplican igual a POST, PUT y
        # PATCH. ``get_queryset`` del viewset acota la OP a la empresa del
        # usuario, así que comparar contra ella equivale a comparar contra la OP.
        if pedido is None:
            return pedido
        return self._validar_pedido(pedido, ya_ligado=(
            self.instance is not None and self.instance.pedido_id == pedido.pk
        ))

    def _validar_pedido(self, pedido, ya_ligado=False):
        request = self.context.get('request')
        empresa_id = getattr(getattr(request, 'user', None), 'empresa_id', None)
        if pedido.empresa_id != empresa_id:
            # Mismo mensaje que un pk inexistente: no revela que existe en otra empresa.
            raise serializers.ValidationError(
                self.fields['pedido'].error_messages['does_not_exist'].format(pk_value=pedido.pk)
            )
        if ya_ligado:
            return pedido

        from produccion.api.views import _detalles_especiales_qs
        if not _detalles_especiales_qs().filter(pedido=pedido).exists():
            raise serializers.ValidationError(
                'El pedido no tiene ninguna línea de producción especial (muestra).'
            )
        if not pedido.clasificacion or not pedido.fecha_confirmacion:
            raise serializers.ValidationError(
                'El pedido debe estar clasificado y con fecha de confirmación antes de ligarlo a una OP.'
            )
        return pedido

    def validate_ruta_produccion(self, ruta_produccion):
        return _fk_de_la_empresa_del_usuario(self, 'ruta_produccion', ruta_produccion)

    def validate_centro_confeccion(self, proveedor):
        proveedor = _fk_de_la_empresa_del_usuario(self, 'centro_confeccion', proveedor)
        if proveedor is not None and not (proveedor.activo and proveedor.es_maquilero):
            raise serializers.ValidationError('El proveedor no está activo como maquilero.')
        return proveedor

    def validate(self, attrs):
        # ``empresa``/``sucursal`` son read-only (ver ``Meta``): nunca llegan
        # aquí desde el body, así que no hay nada que validar contra ellos en
        # ``attrs``. La vista es quien las resuelve del usuario autenticado
        # después de ``is_valid()`` (mismo patrón que OB/OR).
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request else None
        user_empresa = getattr(user, 'empresa', None)

        if not user_empresa:
            raise serializers.ValidationError('Usuario sin empresa asignada')

        # Si la OP tiene ``pedido`` (el del body o, en PATCH, el de la
        # instancia), cada ``pedido_detalle`` de sus renglones debe ser de ESE
        # pedido. La empresa del renglón ya la validó el serializer hijo. Sin
        # ``pedido`` en la OP no se exige nada más: rechazar o no esa
        # combinación es una decisión de negocio pendiente.
        pedido = attrs['pedido'] if 'pedido' in attrs else getattr(self.instance, 'pedido', None)
        # Varios pedidos: sin principal explícito, el principal es el primero.
        if pedido is None and attrs.get('pedidos_ids'):
            pedido = attrs['pedido'] = attrs['pedidos_ids'][0]
        if 'pedidos_ids' in attrs:
            permitidos = {p.pk for p in attrs['pedidos_ids']}
        elif self.instance is not None:
            permitidos = set(self.instance.op_pedidos.values_list('pedido_id', flat=True))
        else:
            permitidos = set()
        if pedido is not None:
            permitidos.add(pedido.pk)
            errores = {}
            errores_renglones = [
                {'pedido_detalle': ['El renglón no pertenece a los pedidos de la orden de producción.']}
                if renglon.get('pedido_detalle') is not None
                and renglon['pedido_detalle'].pedido_id not in permitidos
                else {}
                for renglon in attrs.get('orden_produccion_detalle', [])
            ]
            if any(errores_renglones):
                errores['orden_produccion_detalle'] = errores_renglones
            # Al fijar o cambiar el ``pedido`` de una OP existente, los
            # renglones YA guardados entran en la misma regla: sin esto bastaba
            # crear la OP sin pedido y ligarla después para evadirla.
            if (
                self.instance is not None
                and self.instance.orden_produccion_detalle
                .filter(pedido_detalle__isnull=False)
                .exclude(pedido_detalle__pedido_id__in=permitidos)
                .exists()
            ):
                errores['pedido'] = [
                    'La orden de producción tiene renglones ligados a líneas de otro pedido.'
                ]
            if errores:
                raise serializers.ValidationError(errores)

        return attrs

    def update(self, instance, validated_data):
        # #1090: una OP terminada o cancelada ya no cambia de pedidos.
        E = OrdenProduccion.EstatusOrdenProduccion
        cambia_pedido = 'pedido' in validated_data and validated_data['pedido'] != instance.pedido
        if instance.estatus_op in (E.COMPLETADO, E.CANCELADO) and (
            validated_data.get('pedidos_ids') is not None or cambia_pedido
        ):
            raise serializers.ValidationError(
                {'pedidos_ids': f'La OP está {instance.get_estatus_op_display().lower()}: ya no se pueden cambiar sus pedidos.'}
            )
        pedidos = validated_data.pop('pedidos_ids', None)
        instance = super().update(instance, validated_data)
        if pedidos is not None or 'pedido' in validated_data:
            ligar_pedidos_op(instance, pedidos)
        return instance

    class Meta:
        model = OrdenProduccion
        fields = '__all__'
        read_only_fields = ['folio_op', 'activo', 'usuario_asignado', 'empresa', 'sucursal', 'estatus_op']
        extra_kwargs = {"costo_maquila_unitario": {"min_value": 0}}  # MXN por pieza (#809)


class OrdenProduccionRutaCriticaSerializer(serializers.ModelSerializer):
    """Serializer del endpoint dedicado de ruta crítica (``/ruta-critica/``).

    Deliberadamente separado de ``OrdenProduccionSerializer``: éste no lo
    declara, así que list/retrieve de la OP no cargan ni serializan esta
    tabla. Los cinco campos ``fecha_*`` sellados por el servidor son ``read_only``:
    el modelo los sella (o limpia) cuando su boolean asociado cambia de valor
    (``OrdenProduccionRutaCritica.save``) -- el cliente solo manda el checkbox.
    """

    op_id = serializers.IntegerField(read_only=True)
    estatus_paquete_tecnico_display = serializers.CharField(
        source="get_estatus_paquete_tecnico_display", read_only=True, allow_null=True
    )

    class Meta:
        model = OrdenProduccionRutaCritica
        exclude = ['op']
        read_only_fields = [
            'fecha_existencia_tela',
            'fecha_sin_existencia_tela',
            'fecha_existencia_avios',
            'fecha_sin_existencia_avios',
            'fecha_kit_completo',
            'updated_at',
        ]

    def validate_cantidad_real_corte(self, value):
        if value is None:
            return value
        if value < 0 or value != value.to_integral_value():
            raise serializers.ValidationError(
                "Debe ser un número entero de piezas, mayor o igual a 0."
            )
        return value

class ConsumoProduccionSerializer(serializers.ModelSerializer):
    detalles = serializers.SerializerMethodField()

    class Meta:
        model = ConsumoProduccion
        fields = ['consumo_produccion_id', 'op', 'detalles']

    def validate_op(self, op):
        return _fk_de_la_empresa_del_usuario(self, 'op', op)

    def get_detalles(self, obj):
        detalles = getattr(obj, 'detalles', None)
        if detalles is None:
            return []
        return [
            {
                'id': detalle.consumo_detalle_id,
                'producto': detalle.producto_id,
                'producto_nombre': getattr(detalle.producto, 'nombre', None),
                'cantidad': str(detalle.cantidad),
            }
            for detalle in detalles.select_related('producto').all()
        ]

class ProductoTerminadoEntradasSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductoTerminadoEntradas
        fields = '__all__'
        # ``select_related``: ``validate_ubicacion`` lee la empresa del almacén.
        extra_kwargs = {
            'ubicacion': {'queryset': Ubicacion.objects.select_related('almacen')},
        }

    # Aislamiento multi-tenant de las tres FKs escribibles. ``Almacen.empresa``
    # y ``Ubicacion.almacen`` son nullable: sin empresa determinable se rechaza.
    def validate_op(self, op):
        return _fk_de_la_empresa_del_usuario(self, 'op', op)

    def validate_almacen(self, almacen):
        return _fk_de_la_empresa_del_usuario(self, 'almacen', almacen)

    def validate_ubicacion(self, ubicacion):
        return _fk_de_la_empresa_del_usuario(
            self, 'ubicacion', ubicacion, getattr(ubicacion, 'almacen', None)
        )

    def validate(self, attrs):
        # Ambos son requeridos (FK NOT NULL y el endpoint no tiene update), así
        # que siempre llegan juntos: la ubicación debe ser de ESE almacén.
        almacen, ubicacion = attrs.get('almacen'), attrs.get('ubicacion')
        if almacen is not None and ubicacion is not None and ubicacion.almacen_id != almacen.pk:
            raise serializers.ValidationError(
                {'ubicacion': ['La ubicación no pertenece al almacén indicado.']}
            )
        return attrs

class OrdenBordadoDetalleSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)
    tipo_servicio_bordado_display = serializers.CharField(
        source="get_tipo_servicio_bordado_display",
        read_only=True,
    )
    tipos_servicio = serializers.SerializerMethodField()
    tipos_servicio_display = serializers.SerializerMethodField()
    ubicaciones = serializers.SerializerMethodField()
    foto = serializers.SerializerMethodField()
    notas = serializers.SerializerMethodField()
    bordado_config = serializers.SerializerMethodField()

    class Meta:
        model = OrdenBordadoDetalle
        fields = '__all__'

    def get_tipos_servicio(self, obj):
        """SSoT desde Ventas: ``PedidoDetalleTalla.bordado_config.tipos_servicio``.

        Ruta completa:
          ``OrdenBordadoDetalle.pedido_detalle_id + talla_id``
          → ``ventas.PedidoDetalleTalla`` (único por par, con cache)
          → ``pdt.bordado_config["tipos_servicio"]`` (array strings)

        Fallback legacy: si la OB es vieja y no se pudo vincular al PDT,
        regresamos el campo propio del detalle que ya guardamos en 0033 por
        compatibilidad histórica.
        """
        pdt = self._get_pedido_detalle_talla(obj)
        if pdt is not None and isinstance(getattr(pdt, "bordado_config", None), dict):
            ts = pdt.bordado_config.get("tipos_servicio")
            if isinstance(ts, list):
                return ts
        bruto = getattr(obj, "tipos_servicio", None) or []
        return bruto if isinstance(bruto, list) else []

    def get_tipos_servicio_display(self, obj):
        """Labels traducidos (con acentos) a partir de la SSoT de Ventas."""
        from ventas.servicios_bordado import tipos_servicio_display_list
        return tipos_servicio_display_list(self.get_tipos_servicio(obj))

    def _get_pedido_detalle_talla(self, obj):
        """La ``PedidoDetalleTalla`` de origen de este renglón.

        De ella sólo se lee ``bordado_config`` (aquí abajo y en
        ``get_tipos_servicio``), así que basta con un portador de ese atributo.

        ``context["pdt_bordado_config_map"]`` —cuando existe— es
        ``{(pedido_detalle_id, talla_id): bordado_config}`` para TODO el pedido,
        resuelto en UNA query por ``OrdenBordadoViewSet.retrieve()``. Sin él
        este método consultaba una fila por renglón: ``_pdt_cache`` comparte
        instancia entre filas (DRF reusa el serializer hijo con ``many=True``),
        pero la clave ``(pedido_detalle_id, talla_id)`` es única por renglón por
        construcción, así que nunca acertaba.

        La query por fila se conserva como fallback para cualquier llamador que
        no inyecte el mapa (``create``, anidaciones, comandos).
        """
        if obj.pedido_detalle_id is None or obj.talla_id is None:
            return None
        if not hasattr(self, '_pdt_cache'):
            self._pdt_cache = {}
        key = (obj.pedido_detalle_id, obj.talla_id)
        if key not in self._pdt_cache:
            mapa = self.context.get("pdt_bordado_config_map")
            if mapa is not None:
                # Ausente en el mapa == no existe la fila: mismo ``None`` que
                # devolvía ``.first()``.
                self._pdt_cache[key] = (
                    SimpleNamespace(bordado_config=mapa[key])
                    if key in mapa
                    else None
                )
            else:
                from ventas.models import PedidoDetalleTalla
                self._pdt_cache[key] = (
                    PedidoDetalleTalla.objects
                    .filter(pedido_detalle_id=obj.pedido_detalle_id, talla_id=obj.talla_id)
                    .only('bordado_config')
                    .first()
                )
        return self._pdt_cache[key]

    def _get_cfg(self, obj):
        """El ``bordado_config`` CRUDO, tal cual lo guardó ventas."""
        pdt = self._get_pedido_detalle_talla(obj)
        return getattr(pdt, 'bordado_config', None) or {}

    def _get_cfg_dict(self, obj):
        """El config sólo si es un dict; ``{}`` en cualquier otro caso.

        Mismo par crudo/normalizado que ``OrdenReflejanteDetalleSerializer``:
        ``get_bordado_config`` publica el valor ÍNTEGRO (no puede normalizarse
        sin perder dato), mientras que las tres extracciones de abajo necesitan
        un dict para llamar ``.get()``.

        Hoy ``bordado_config`` es un objeto en el 100% de las filas (96/96), así
        que esto es un no-op; el guardia existe porque este mismo desajuste
        lista/objeto ya causó tres 500 en reflejante y esta era la copia del
        mismo código que quedó sin proteger.
        """
        return config_como_dict(self._get_cfg(obj))

    def get_bordado_config(self, obj):
        cfg = self._get_cfg(obj)
        return cfg or None

    def get_ubicaciones(self, obj):
        cfg = self._get_cfg_dict(obj)
        ubicaciones = cfg.get('ubicaciones')
        return ubicaciones if isinstance(ubicaciones, list) else []

    def get_foto(self, obj):
        cfg = self._get_cfg_dict(obj)
        for key in ('foto', 'imagen', 'imagen_url', 'foto_url'):
            value = cfg.get(key)
            if value:
                if isinstance(value, dict):
                    return value
                return {'url': value}
        return None

    def get_notas(self, obj):
        cfg = self._get_cfg_dict(obj)
        for key in ('notas', 'observaciones', 'comentarios'):
            value = cfg.get(key)
            if value:
                return value
        return None

class OrdenBordadoSerializer(serializers.ModelSerializer):
    pedido_folio = serializers.CharField(source='pedido.folio', read_only=True)
    detalles = OrdenBordadoDetalleSerializer(many=True, read_only=True)
    # ``empresa``/``sucursal`` siguen viajando como id (read_only en Meta); estos
    # dos son etiquetas legibles adicionales, mismo patrón ``source=`` que
    # ``pedido_folio`` (ver ``OrdenReflejanteSerializer``). ``Empresa`` no tiene
    # campo ``nombre``: su nombre humano es ``razon_social``.
    empresa_nombre = serializers.CharField(source='empresa.razon_social', read_only=True)
    sucursal_nombre = serializers.CharField(source='sucursal.nombre', read_only=True)
    usuario_nombre = serializers.SerializerMethodField()
    estatus_bordado_display = serializers.CharField(source="get_estatus_bordado_display", read_only=True)
    proveedor_nombre = serializers.CharField(
        source="proveedor.nombre", read_only=True, default=None,
    )
    proveedor_display = serializers.SerializerMethodField()

    detalles_override = serializers.ListField(
        child=serializers.JSONField(),
        required=False,
        write_only=True,
        allow_null=True,
    )

    class Meta:
        model = OrdenesBordado
        fields = '__all__'
        read_only_fields = [
            'activo',
            'folio_bordado',
            'empresa',
            'sucursal',
        ]

    def get_usuario_nombre(self, obj):
        usuario = obj.usuario_asignado
        if not usuario: return None
        return usuario.get_full_name().strip() or usuario.email

    def get_proveedor_display(self, obj):
        """Label compacto del proveedor (para chip UI)."""
        prov = getattr(obj, "proveedor", None)
        if prov is None:
            return None
        return {
            "id": prov.pk,
            "codigo": getattr(prov, "codigo", None),
            "nombre": getattr(prov, "nombre", None),
            "razon_social": getattr(prov, "razon_social", None),
            "tipo": getattr(prov, "tipo", None),
            "rfc": getattr(prov, "rfc", None),
            "email": getattr(prov, "email", None),
            "telefono": getattr(prov, "telefono", None),
            "contacto_principal": getattr(prov, "contacto_principal", None),
        }

    def validate_pedido(self, pedido):
        return _pedido_en_alcance(self, pedido, "bordado", "BORDADO")

    def validate(self, attrs):
        """Valida cross-tenant de ``proveedor`` y ``detalles_override``.

        Si el body selecciona un proveedor, su ``empresa`` debe ser NULL
        (catálogo global) o coincidir con la empresa asignada a la OB. ``Proveedor.empresa``
        es nullable en el modelo por compatibilidad histórica, así que permitimos
        ambos casos. Para OB NUEVA se compara con ``attrs["empresa"]``; para un
        ``PATCH``/update se compara con ``self.instance.empresa_id`` (cuando
        attrs no redefine ``empresa``, que es read_only).
        """
        # --- Cross-tenant: proveedor vs empresa de la OB ---------------------
        proveedor = attrs.get("proveedor", None)
        if proveedor is not None:
            if "empresa" in attrs:
                empresa_id = getattr(attrs["empresa"], "pk", None) or attrs["empresa"]
            elif self.instance is not None:
                empresa_id = getattr(self.instance, "empresa_id", None)
            else:
                empresa_id = None
            prov_empresa_id = getattr(proveedor, "empresa_id", None)
            if (
                prov_empresa_id is not None
                and empresa_id is not None
                and int(prov_empresa_id) != int(empresa_id)
            ):
                raise serializers.ValidationError({
                    "proveedor": (
                        f"Proveedor id={proveedor.pk} pertenece a empresa "
                        f"{prov_empresa_id}, no coincide con empresa de la OB "
                        f"{empresa_id}."
                    )
                })

        # --- detalle_override (lo heredamos y lo corremos después -------------
        detalles_override = attrs.get("detalles_override") or []
        pedido = attrs.get("pedido")
        if detalles_override:
            from ventas.models import PedidoDetalleTalla

            seen_ids = set()
            for item in detalles_override:
                if not isinstance(item, dict):
                    raise serializers.ValidationError({
                        "detalles_override": "Cada renglón debe ser un objeto."
                    })

                pdt_id = item.get("pedido_detalle_talla_id")
                cantidad = item.get("cantidad")

                if pdt_id is None:
                    raise serializers.ValidationError({
                        "detalles_override": "Falta `pedido_detalle_talla_id`."
                    })
                if pdt_id in seen_ids:
                    raise serializers.ValidationError({
                        "detalles_override": f"`pedido_detalle_talla_id={pdt_id}` repetido."
                    })
                seen_ids.add(pdt_id)

                try:
                    cantidad_num = float(cantidad)
                except (TypeError, ValueError):
                    raise serializers.ValidationError({
                        "detalles_override": f"`cantidad` inválida para `pedido_detalle_talla_id={pdt_id}`."
                    })
                if cantidad_num <= 0:
                    raise serializers.ValidationError({
                        "detalles_override": f"`cantidad` debe ser mayor a 0 para `pedido_detalle_talla_id={pdt_id}`."
                    })
                # ``PedidoDetalleTalla.cantidad`` es ``PositiveIntegerField``:
                # las prendas se bordan enteras. Aceptar fraccionarios metía
                # residuos de coma flotante en las sumas de cupo y dejaba
                # pendientes de 1e-15 que ninguna OB podía consumir.
                if cantidad_num != int(cantidad_num):
                    raise serializers.ValidationError({
                        "detalles_override": (
                            f"`cantidad` debe ser un número entero de piezas para "
                            f"`pedido_detalle_talla_id={pdt_id}` (llegó {cantidad_num})."
                        )
                    })

                if pedido is not None:
                    # Filtrado por el pedido: un id de otro pedido responde igual que uno
                    # inexistente y no deja enumerar tallas ajenas.
                    try:
                        pdt = PedidoDetalleTalla.objects.select_related("pedido_detalle").get(
                            pk=pdt_id, pedido_detalle__pedido=pedido
                        )
                    except PedidoDetalleTalla.DoesNotExist:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no existe "
                                f"en el pedido `{pedido.pk}`."
                            )
                        })
                    if not pdt.lleva_bordado:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no lleva "
                                "servicio de bordado (`lleva_bordado=False`)."
                            )
                        })
                    if cantidad_num > float(pdt.cantidad or 0):
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}`: la cantidad "
                                f"{cantidad_num} excede la del pedido "
                                f"({float(pdt.cantidad or 0)})."
                            )
                        })
        return attrs

class OrdenBordadoDetalleListSerializer(serializers.ModelSerializer):
    """Renglón de orden de bordado para el listado."""

    # NOTA (comentario, no docstring: drf-spectacular publica el docstring como
    # `description` del componente en /api/schema/ y /api/docs/, y esto es
    # detalle interno de implementación).
    #
    # ``OrdenBordadoDetalleSerializer`` re-lee ``PedidoDetalleTalla`` para
    # resolver ``ubicaciones``/``foto``/``notas``/``bordado_config``, y su caché
    # va por ``(pedido_detalle_id, talla_id)`` —clave que en datos reales es
    # distinta en cada renglón, así que nunca acierta—. Resultado: una query por
    # renglón.
    #
    # Ninguna vista de listado consume esos cuatro campos —los pinta el diálogo
    # de detalle, que sigue usando el serializer completo—, así que aquí
    # simplemente no se declaran: sin ``SerializerMethodField`` no hay lookup, y
    # el listado deja de crecer con el número de renglones.

    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)

    class Meta:
        model = OrdenBordadoDetalle
        # ``exclude`` en vez de ``fields = '__all__'`` sólo por
        # ``configuracion``: es una columna del modelo, así que ``__all__`` la
        # metería sola en el listado. No cuesta queries (viaja en el mismo
        # SELECT), pero sí peso: es el ``bordado_config`` entero, con todas sus
        # ubicaciones y sus URLs de imagen, por renglón. El listado se mantiene
        # ligero y el detalle —que sí la declara— es quien la publica.
        exclude = ('configuracion',)


class _FechasPedidoListadoMixin(serializers.Serializer):
    """``fecha_pedido`` (alta del pedido) y ``fecha_vence`` (la ``fecha_compromiso``
    de pedidos especiales/OP) para los listados de OB/OR/OCM. ``pedido`` ya viene
    en ``select_related``: sin consultas extra."""

    fecha_pedido = serializers.SerializerMethodField()
    fecha_vence = serializers.SerializerMethodField()

    def get_fecha_pedido(self, obj):
        from django.utils import timezone
        pedido = obj.pedido
        return timezone.localdate(pedido.created_at) if pedido and pedido.created_at else None

    def get_fecha_vence(self, obj):
        from ventas.services.clasificacion_service import rango_fecha_entrega
        return rango_fecha_entrega(obj.pedido)[1] if obj.pedido else None


def _num(obj, attr):
    return float(getattr(obj, attr, 0) or 0)


class _ResumenAvancePiezasListadoMixin(_FechasPedidoListadoMixin):
    """``resumen_avance`` del listado de OR/OCM: misma forma que el retrieve.
    Programado = renglones prefetcheados; realizado = ``av_cantidad`` anotada
    en el ``get_queryset`` del listado."""

    resumen_avance = serializers.SerializerMethodField()

    def get_resumen_avance(self, obj):
        programada = sum((float(d.cantidad or 0) for d in obj.detalles.all()), 0.0)
        realizada = _num(obj, "av_cantidad")
        return {
            "cantidad_programada": programada,
            "cantidad_realizada_total": realizada,
            "porcentaje_avance": round(realizada / programada * 100.0, 2) if programada > 0 else 0.0,
        }


class _ResumenAvanceBordadoListadoMixin(_FechasPedidoListadoMixin):
    """``resumen_avance`` del listado de OB: los totales del retrieve
    (``OrdenBordadoRetrieveSerializer.get_resumen_avance``) sin ``por_detalle``.
    Los avances llegan sumados por orden (``av_*``, anotados en el queryset)."""

    resumen_avance = serializers.SerializerMethodField()

    def get_resumen_avance(self, obj):
        detalles = list(obj.detalles.all())
        programada = sum((float(d.cantidad or 0) for d in detalles), 0.0)
        presupuesto = sum(int(d.puntadas or 0) for d in detalles)
        presupuesto_total = sum(int(d.puntadas or 0) * float(d.cantidad or 0) for d in detalles)
        bordada = _num(obj, "av_cantidad")
        puntadas_total = int(_num(obj, "av_puntadas_total"))
        pp_den = _num(obj, "av_pp_den")

        def pct(num, den):
            return round(float(num) / float(den) * 100.0, 2) if den > 0 else 0.0

        return {
            "cantidad_programada": programada,
            "cantidad_bordada_total": bordada,
            "puntadas_presupuesto": presupuesto,
            "puntadas_por_pieza_promedio": int(round(_num(obj, "av_pp_num") / pp_den)) if pp_den > 0 else 0,
            "puntadas_realizadas": int(_num(obj, "av_puntadas_realizadas")),
            "puntadas_total": puntadas_total,
            "porcentaje_avance": pct(bordada, programada),
            "puntadas_porcentaje_avance": pct(puntadas_total, presupuesto_total),
        }


class OrdenBordadoListSerializer(_ResumenAvanceBordadoListadoMixin, OrdenBordadoSerializer):
    """Orden de bordado para el listado."""

    # Hereda de ``OrdenBordadoSerializer`` para que el encabezado no pueda
    # divergir: mismos campos, mismo ``Meta``, mismo ``usuario_nombre``. Lo
    # único que cambia es que ``detalles`` usa el renglón ligero y que se
    # añaden los tres campos de cobertura.
    #
    # La cobertura NO se calcula aquí por fila: el ``ViewSet`` la resuelve para
    # toda la página en 2 queries agrupadas y la deja en el contexto bajo
    # ``cobertura`` (ver ``OrdenBordadoService.cobertura_por_orden``). Si cada
    # fila la calculara sola volveríamos al N+1 que este listado acaba de
    # quitarse de encima.

    detalles = OrdenBordadoDetalleListSerializer(many=True, read_only=True)

    cantidad_cubierta = serializers.SerializerMethodField()
    cantidad_contratada = serializers.SerializerMethodField()
    cobertura_completa = serializers.SerializerMethodField()

    def _cobertura(self, obj):
        # Se exige la clave, no se asume: sin ella los tres getters devolvían
        # 0/0/False —una respuesta bien formada afirmando que la orden no cubre
        # nada de nada—, indistinguible de un dato real. Cualquier uso de este
        # serializer fuera de ``OrdenBordadoViewSet.list``/``retrieve`` (una
        # anidación, una acción nueva, un comando) publicaría ceros como hechos.
        # Se prefiere fallar de forma visible. ``.get(obj.pk)`` sí puede faltar
        # legítimamente —una orden ajena a la página— y ahí el default es
        # correcto.
        if "cobertura" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'cobertura' en el contexto; "
                "lo inyecta OrdenBordadoViewSet (ver cobertura_por_orden)."
            )
        return (self.context["cobertura"] or {}).get(obj.pk) or {}

    def get_cantidad_cubierta(self, obj):
        """Piezas que programa ESTA orden."""
        return self._cobertura(obj).get("cubierto", 0)

    def get_cantidad_contratada(self, obj):
        """Piezas de bordado que contrató el pedido (todas sus líneas)."""
        return self._cobertura(obj).get("contratado", 0)

    def get_cobertura_completa(self, obj):
        """¿Esta orden sola cubre el 100% de lo contratado por el pedido?"""
        return self._cobertura(obj).get("completa", False)


class OrdenBordadoDetalleRetrieveSerializer(_SkuPorTallaMixin, OrdenBordadoDetalleSerializer):
    """Renglón del DETALLE, con el contexto de parcialidad de su línea."""

    # Mismos nombres que ya usa el GET de onboarding para estos tres conceptos
    # (``cantidad_pedido``/``cantidad_asignada``/``cantidad_pendiente``), para
    # no inventar un vocabulario paralelo. Aquí ``cantidad`` sigue siendo lo que
    # programa ESTA orden; los tres nuevos hablan del pedido y de TODAS sus OBs
    # activas.
    #
    # El ``ViewSet`` deja el mapa por línea en el contexto: una sola resolución
    # para todos los renglones, sin query por fila.

    cantidad_pedido = serializers.SerializerMethodField()
    cantidad_asignada = serializers.SerializerMethodField()
    cantidad_pendiente = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()

    def _linea(self, obj):
        """``(pedido, asignada, pendiente)`` de este renglón.

        Se exige la clave del contexto por el mismo motivo que
        ``OrdenBordadoListSerializer._cobertura``: sin ella los tres getters
        devolvían ``null`` en silencio.

        Un renglón con ``talla`` NULL —los genera el pipeline de picking— no
        tiene entrada por talla, porque el mapa se construye desde
        ``PedidoDetalleTalla``, que siempre la trae. Antes eso daba tres
        ``null`` justo en el caso que ``reparto_por_talla_aproximado`` dice
        estar describiendo: la bandera anunciaba "reparto aproximado" y el
        renglón no mostraba número alguno. Ahora se cae al total del
        ``pedido_detalle``, que sí es exacto (es lo que garantiza
        ``pendientes_por_linea``), de modo que la bandera y los números hablan
        de lo mismo.
        """
        if "partialidad_por_linea" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'partialidad_por_linea' en el "
                "contexto; lo inyecta OrdenBordadoViewSet.retrieve "
                "(ver partialidad_de_orden)."
            )
        mapa = self.context["partialidad_por_linea"] or {}
        linea = mapa.get((obj.pedido_detalle_id, obj.talla_id))
        if linea is not None:
            return linea
        por_detalle = self.context.get("partialidad_por_detalle") or {}
        return por_detalle.get(obj.pedido_detalle_id)

    def get_cantidad_pedido(self, obj):
        """Piezas contratadas por el pedido en esta línea.

        Para un renglón sin talla identificable, el total del ``pedido_detalle``.
        """
        linea = self._linea(obj)
        return linea[0] if linea else None

    def get_cantidad_asignada(self, obj):
        """Piezas ya programadas en esta línea por TODAS las OBs activas."""
        linea = self._linea(obj)
        return linea[1] if linea else None

    def get_cantidad_pendiente(self, obj):
        """Saldo de la línea: ``cantidad_pedido - cantidad_asignada``."""
        linea = self._linea(obj)
        return linea[2] if linea else None


class OrdenBordadoHermanaSerializer(serializers.Serializer):
    """Referencia mínima a otra OB activa del mismo pedido.

    Existe para que ``fecha_inicio`` pase por un ``DateTimeField`` de DRF. El
    servicio la trae con ``.values(...)``, así que llegaba como ``datetime``
    crudo y el renderer JSON la emitía en un formato distinto (UTC, sufijo
    ``Z``) al de la ``fecha_inicio`` de la propia orden, que sí es campo de
    modelo y respeta ``DATETIME_FORMAT`` y la zona activa. Dos formas distintas
    del mismo instante, bajo el mismo nombre, en una sola respuesta.
    """

    id = serializers.IntegerField(read_only=True)
    folio_bordado = serializers.CharField(read_only=True)
    fecha_inicio = serializers.DateTimeField(read_only=True)


class _OrdenBordadoAvanceReadSerializer(serializers.ModelSerializer):
    usuario_nombre = serializers.SerializerMethodField()
    orden_bordado_detalle_display = serializers.SerializerMethodField()
    pedido_detalle_talla_display = serializers.SerializerMethodField()

    class Meta:
        model = BordadoAvances
        fields = [
            "id",
            "fecha",
            "usuario",
            "usuario_nombre",
            "orden_bordado_detalle",
            "orden_bordado_detalle_display",
            "pedido_detalle_talla",
            "pedido_detalle_talla_display",
            "cantidad_bordada",
            "puntadas_por_pieza",
            "puntadas_realizadas",
            "puntadas_total",
            "comentario",
        ]
        read_only_fields = fields

    def get_usuario_nombre(self, obj):
        usuario = getattr(obj, "usuario", None)
        if not usuario:
            return None
        return usuario.get_full_name().strip() or getattr(usuario, "email", None)

    def get_orden_bordado_detalle_display(self, obj):
        det = getattr(obj, "orden_bordado_detalle", None)
        if not det:
            return None
        producto = getattr(det, "producto", None)
        talla = getattr(det, "talla", None)
        color = getattr(det, "color", None)
        return {
            "id": det.pk,
            "producto_id": getattr(det, "producto_id", None),
            "producto_nombre": getattr(producto, "nombre", None),
            "talla_id": getattr(det, "talla_id", None),
            "talla_nombre": getattr(talla, "nombre", None) if talla else None,
            "color_id": getattr(det, "color_id", None),
            "color_nombre": getattr(color, "nombre", None) if color else None,
            "cantidad_programada": getattr(det, "cantidad", None),
            "posicion_bordado": getattr(det, "posicion_bordado", None),
        }

    def get_pedido_detalle_talla_display(self, obj):
        pdt = getattr(obj, "pedido_detalle_talla", None)
        if not pdt:
            return None
        talla = getattr(pdt, "talla", None)
        return {
            "id": pdt.pk,
            "pedido_detalle_id": getattr(pdt, "pedido_detalle_id", None),
            "talla_id": getattr(pdt, "talla_id", None),
            "talla_nombre": getattr(talla, "nombre", None) if talla else None,
            "cantidad_pedido": getattr(pdt, "cantidad", None),
        }


class OrdenBordadoRetrieveSerializer(OrdenBordadoListSerializer):
    """Orden de bordado para el detalle, con contexto de parcialidad.

    Hereda de ``OrdenBordadoListSerializer`` —no de ``OrdenBordadoSerializer``—
    para conservar los tres campos de cobertura
    (``cantidad_cubierta``/``cantidad_contratada``/``cobertura_completa``). Sin
    ellos el detalle no podía enunciar "cubre 7 de 40": los campos por línea no
    sustituyen al total, porque el detalle sólo itemiza las líneas que ESTA
    orden toca —la OB 61 muestra 2 de las 6 del pedido, y sus
    ``cantidad_pedido`` suman 20, no 40—. Un diálogo montado sobre el GET por id
    tendría que leer además la fila del listado, que es justo lo que el fetch
    por id venía a evitar.

    ``create`` conserva ``OrdenBordadoSerializer`` tal cual, así que el alta no
    cambia lo que devuelve.
    """

    # Vuelve a apuntar al renglón completo: ``OrdenBordadoListSerializer`` lo
    # deja en el ligero (sin ``ubicaciones``/``foto``/``notas``/
    # ``bordado_config``), que es correcto para el listado pero no para el
    # detalle.
    detalles = OrdenBordadoDetalleRetrieveSerializer(many=True, read_only=True)

    otras_ordenes_del_pedido = serializers.SerializerMethodField()
    reparto_por_talla_aproximado = serializers.SerializerMethodField()
    pedido_vinculado = serializers.SerializerMethodField()
    avances = serializers.SerializerMethodField()
    resumen_avance = serializers.SerializerMethodField()

    def get_pedido_vinculado(self, obj):
        from produccion.services.orden_bordado_field_filter_service import armar_pedido_vinculado
        return armar_pedido_vinculado(obj)

    def get_otras_ordenes_del_pedido(self, obj):
        """Las demás OBs activas del mismo pedido, sin incluir ésta."""
        # Se exige la clave: un ``[]`` por contexto ausente es indistinguible de
        # una orden que de verdad no tiene hermanas. Ver ``_cobertura``.
        if "hermanas" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'hermanas' en el contexto; "
                "lo inyecta OrdenBordadoViewSet.retrieve "
                "(ver partialidad_de_orden)."
            )
        return OrdenBordadoHermanaSerializer(
            self.context["hermanas"] or [], many=True
        ).data

    def get_reparto_por_talla_aproximado(self, obj):
        """¿El reparto por talla es aproximado?

        ``True`` cuando el pedido tiene piezas programadas sin talla
        identificable (renglones con ``talla`` NULL, que genera el pipeline de
        picking). El total por ``pedido_detalle`` sigue siendo exacto; lo que
        no se puede afirmar es a qué talla concreta pertenecen. Hoy no hay
        ningún renglón así en la base.
        """
        return bool(self.context.get("reparto_aproximado", False))

    def get_avances(self, obj):
        avances = self.context.get("avances") or []
        return _OrdenBordadoAvanceReadSerializer(avances, many=True).data

    def get_resumen_avance(self, obj):
        avances = self.context.get("avances") or []

        detalles = getattr(obj, "detalles", None)
        if detalles is None:
            detalles_qs = (
                OrdenBordadoDetalle.objects
                .filter(ob_id=obj.pk)
                .only("id", "cantidad", "puntadas", "producto_id", "talla_id", "color_id",
                      "pedido_detalle_id", "posicion_bordado")
                .select_related("producto", "talla", "color")
            )
        else:
            try:
                detalles_qs = list(detalles.all())
            except Exception:
                detalles_qs = list(detalles)

        cantidad_programada = sum(
            float(getattr(d, "cantidad", 0) or 0) for d in detalles_qs
        )
        puntadas_presupuesto = sum(
            int(getattr(d, "puntadas", 0) or 0) for d in detalles_qs
        )
        # Presupuesto REAL de puntadas de toda la OB: ``OrdenBordadoDetalle
        # .puntadas`` es POR PIEZA (va de la mano de ``cantidad``), así que el
        # total programado es Σ(puntadas_i × cantidad_i) — no la suma pelada de
        # ``puntadas``, que sólo presupuesta una prenda por renglón. No se
        # expone: es únicamente el denominador de ``puntadas_porcentaje_avance``
        # (``puntadas_presupuesto`` sigue publicándose tal cual, por pieza).
        puntadas_presupuesto_total = sum(
            int(getattr(d, "puntadas", 0) or 0) * float(getattr(d, "cantidad", 0) or 0)
            for d in detalles_qs
        )

        if not avances:
            por_detalle = []
            for d in detalles_qs:
                producto = getattr(d, "producto", None)
                talla = getattr(d, "talla", None)
                color = getattr(d, "color", None)
                pp = int(getattr(d, "puntadas", 0) or 0)
                por_detalle.append({
                    "orden_bordado_detalle_id": d.pk,
                    "producto_id": getattr(d, "producto_id", None),
                    "producto_nombre": getattr(producto, "nombre", None),
                    "talla_id": getattr(d, "talla_id", None),
                    "talla_nombre": getattr(talla, "nombre", None) if talla else None,
                    "color_id": getattr(d, "color_id", None),
                    "color_nombre": getattr(color, "nombre", None) if color else None,
                    "posicion_bordado": getattr(d, "posicion_bordado", None),
                    "cantidad_programada": float(getattr(d, "cantidad", 0) or 0),
                    "puntadas_presupuesto": pp,
                    "cantidad_bordada": 0.0,
                    "puntadas_por_pieza_promedio": 0,
                    "puntadas_realizadas": 0,
                    "puntadas_total": 0,
                    "porcentaje_avance": 0.0,
                    "puntadas_porcentaje_avance": 0.0,
                    "operadores": [],
                })
            return {
                "cantidad_programada": cantidad_programada,
                "cantidad_bordada_total": 0.0,
                "puntadas_presupuesto": puntadas_presupuesto,
                "puntadas_por_pieza_promedio": 0,
                "puntadas_realizadas": 0,
                "puntadas_total": 0,
                "porcentaje_avance": 0.0,
                "puntadas_porcentaje_avance": 0.0,
                "por_detalle": por_detalle,
            }

        cantidad_bordada = sum(
            float(getattr(a, "cantidad_bordada", 0) or 0) for a in avances
        )
        puntadas_realizadas = sum(
            int(getattr(a, "puntadas_realizadas", 0) or 0) for a in avances
        )
        puntadas_total = sum(
            int(getattr(a, "puntadas_total", 0) or 0) for a in avances
        )
        # Promedio PONDERADO de puntadas_por_pieza para toda la OB:
        #    sum(por_pieza * cantidad) / sum(cantidad)
        numerador_pp = 0.0
        denominador_pp = 0.0
        for a in avances:
            pp = int(getattr(a, "puntadas_por_pieza", 0) or 0)
            qty = float(getattr(a, "cantidad_bordada", 0) or 0)
            if pp > 0 and qty > 0:
                numerador_pp += pp * qty
                denominador_pp += qty
        puntadas_por_pieza_promedio = (
            int(round(numerador_pp / denominador_pp)) if denominador_pp > 0 else 0
        )

        pct = 0.0
        if cantidad_programada > 0:
            pct = round((cantidad_bordada / cantidad_programada) * 100.0, 2)

        # === NUEVA MÉTRICA 2.0.1: progreso POR PUNTADAS (no por piezas) =====
        # Regla del cliente:
        #   puntadas_porcentaje_avance = puntadas_total / Σ(puntadas × cantidad) × 100
        # Es una métrica más fina que porcentaje_avance porque una prenda a la
        # mitad (4000/8000 puntadas) puede reportarse como 0 piezas terminadas.
        # El denominador va ESCALADO por las piezas programadas: el numerador
        # (``BordadoAvances.puntadas_total`` = por_pieza × cantidad_bordada)
        # acumula las puntadas de TODAS las prendas bordadas, así que dividir
        # entre el presupuesto de una sola prenda daba porcentajes >100%
        # (2 de 10 piezas a 5000 puntadas: 10000/5000 = 200% en vez de 20%).
        puntadas_porcentaje_avance = 0.0
        if puntadas_presupuesto_total > 0:
            puntadas_porcentaje_avance = round(
                (float(puntadas_total) / float(puntadas_presupuesto_total)) * 100.0,
                2,
            )

        def pct_de(num, den):
            return round(float(num) / float(den) * 100.0, 2) if den and den > 0 else 0.0

        agrupado = {}
        for a in avances:
            key = getattr(a, "orden_bordado_detalle_id", None)
            bucket = agrupado.setdefault(key, {
                "cantidad_bordada": 0.0,
                "puntadas_por_pieza_num": 0.0,
                "puntadas_por_pieza_den": 0.0,
                "puntadas_realizadas": 0,
                "puntadas_total": 0,
                "operadores": {},
            })
            qty = float(getattr(a, "cantidad_bordada", 0) or 0)
            pts = int(getattr(a, "puntadas_realizadas", 0) or 0)
            ptotal = int(getattr(a, "puntadas_total", 0) or 0)
            pp = int(getattr(a, "puntadas_por_pieza", 0) or 0)
            bucket["cantidad_bordada"] += qty
            bucket["puntadas_realizadas"] += pts
            bucket["puntadas_total"] += ptotal
            if pp > 0 and qty > 0:
                bucket["puntadas_por_pieza_num"] += pp * qty
                bucket["puntadas_por_pieza_den"] += qty
            usr = getattr(a, "usuario", None)
            uid = getattr(usr, "pk", None) if usr else None
            if uid is not None:
                if uid not in bucket["operadores"]:
                    nombre = (
                        getattr(usr, "get_full_name", lambda: "")().strip()
                        or getattr(usr, "email", "")
                    )
                    bucket["operadores"][uid] = {
                        "usuario_id": uid,
                        "usuario_nombre": nombre,
                        "cantidad_bordada": 0.0,
                        "puntadas_por_pieza_num": 0.0,
                        "puntadas_por_pieza_den": 0.0,
                        "puntadas_realizadas": 0,
                        "puntadas_total": 0,
                    }
                bucket["operadores"][uid]["cantidad_bordada"] += qty
                bucket["operadores"][uid]["puntadas_realizadas"] += pts
                bucket["operadores"][uid]["puntadas_total"] += ptotal
                if pp > 0 and qty > 0:
                    bucket["operadores"][uid]["puntadas_por_pieza_num"] += pp * qty
                    bucket["operadores"][uid]["puntadas_por_pieza_den"] += qty

        def _prom_pp(bucket):
            den = bucket.get("puntadas_por_pieza_den", 0.0) or 0.0
            num = bucket.get("puntadas_por_pieza_num", 0.0) or 0.0
            return int(round(num / den)) if den > 0 else 0

        def _aplanar_operadores(ops_map):
            lista = []
            for op in ops_map.values():
                op2 = dict(op)
                op2["puntadas_por_pieza_promedio"] = _prom_pp(op2)
                op2.pop("puntadas_por_pieza_num", None)
                op2.pop("puntadas_por_pieza_den", None)
                lista.append(op2)
            return lista

        por_detalle = []
        for d in detalles_qs:
            key = d.pk
            bucket = agrupado.get(key, {
                "cantidad_bordada": 0.0,
                "puntadas_por_pieza_num": 0.0,
                "puntadas_por_pieza_den": 0.0,
                "puntadas_realizadas": 0,
                "puntadas_total": 0,
                "operadores": {},
            })
            programa = float(getattr(d, "cantidad", 0) or 0)
            pp = int(getattr(d, "puntadas", 0) or 0)
            porc = 0.0
            if programa > 0:
                porc = round((bucket["cantidad_bordada"] / programa) * 100.0, 2)
            # Mismo escalado que la métrica global: ``pp`` es el presupuesto de
            # UNA prenda, el denominador del renglón es pp × piezas programadas.
            # El guard cubre los dos factores: basta que ``pp`` o ``programa``
            # sea 0 para que el producto lo sea y se devuelva 0.0.
            porc_punt = 0.0
            presupuesto_linea = float(pp) * programa
            if presupuesto_linea > 0:
                porc_punt = round(
                    (float(bucket["puntadas_total"]) / presupuesto_linea) * 100.0,
                    2,
                )
            producto = getattr(d, "producto", None)
            talla = getattr(d, "talla", None)
            color = getattr(d, "color", None)
            por_detalle.append({
                "orden_bordado_detalle_id": d.pk,
                "producto_id": getattr(d, "producto_id", None),
                "producto_nombre": getattr(producto, "nombre", None),
                "talla_id": getattr(d, "talla_id", None),
                "talla_nombre": getattr(talla, "nombre", None) if talla else None,
                "color_id": getattr(d, "color_id", None),
                "color_nombre": getattr(color, "nombre", None) if color else None,
                "posicion_bordado": getattr(d, "posicion_bordado", None),
                "cantidad_programada": programa,
                "puntadas_presupuesto": pp,
                "cantidad_bordada": bucket["cantidad_bordada"],
                "puntadas_por_pieza_promedio": _prom_pp(bucket),
                "puntadas_realizadas": bucket["puntadas_realizadas"],
                "puntadas_total": bucket["puntadas_total"],
                "porcentaje_avance": porc,
                "puntadas_porcentaje_avance": porc_punt,
                "operadores": _aplanar_operadores(bucket["operadores"] or {}),
            })

        avance_sin_detalle = agrupado.get(None, None)
        if avance_sin_detalle and (
            avance_sin_detalle["cantidad_bordada"] or avance_sin_detalle["puntadas_realizadas"]
            or avance_sin_detalle.get("puntadas_total")
        ):
            # Captura simple: el avance se registra por OB (sin talla/SKU), así
            # que este renglón se mide contra el programa total de la OB.
            programa = cantidad_programada
            porc = pct_de(avance_sin_detalle["cantidad_bordada"], programa)
            porc_punt = pct_de(
                avance_sin_detalle.get("puntadas_total", 0) or 0,
                puntadas_presupuesto_total,
            )
            por_detalle.append({
                "orden_bordado_detalle_id": None,
                "producto_id": None,
                "producto_nombre": "Avance de la OB (sin talla)",
                "talla_id": None,
                "talla_nombre": None,
                "color_id": None,
                "color_nombre": None,
                "posicion_bordado": None,
                "cantidad_programada": programa,
                "puntadas_presupuesto": 0,
                "cantidad_bordada": avance_sin_detalle["cantidad_bordada"],
                "puntadas_por_pieza_promedio": _prom_pp(avance_sin_detalle),
                "puntadas_realizadas": avance_sin_detalle["puntadas_realizadas"],
                "puntadas_total": avance_sin_detalle.get("puntadas_total", 0) or 0,
                "porcentaje_avance": porc,
                "puntadas_porcentaje_avance": porc_punt,
                "operadores": _aplanar_operadores(avance_sin_detalle.get("operadores") or {}),
            })

        return {
            "cantidad_programada": cantidad_programada,
            "cantidad_bordada_total": cantidad_bordada,
            "puntadas_presupuesto": puntadas_presupuesto,
            "puntadas_por_pieza_promedio": puntadas_por_pieza_promedio,
            "puntadas_realizadas": puntadas_realizadas,
            "puntadas_total": puntadas_total,
            "porcentaje_avance": pct,
            "puntadas_porcentaje_avance": puntadas_porcentaje_avance,
            "por_detalle": por_detalle,
        }


class _OrdenPadreWriteOnceMixin:
    """Endurece la superficie escribible de los serializers satélite
    (Avances/Incidencias de Bordado y Reflejante), que declaran
    ``fields = '__all__'``.

    Dos candados, sobre la misma línea que ``OrdenReflejanteSerializer``:

    - ``activo`` va en ``read_only_fields`` de cada ``Meta``: el borrado es
      lógico y sólo lo togglea ``perform_destroy`` del ViewSet (soft delete).
      No debe poder apagarse mandando ``activo=false`` en un PATCH.
    - La FK a la orden padre (``ob`` / ``orden_r``) es **write-once**: se fija
      al crear y se ignora en update. Mover un avance/incidencia a otra orden
      —potencialmente de otro tenant, ya que aquí no se revalida la nueva FK—
      no es una operación legítima. Sigue el idioma del repo de descartar
      claves inmutables con ``validated_data.pop`` en ``update`` (ver
      ``inventarios``/``ListaMaterialBomSerializer``).

    ``usuario`` (autoría) se deja escribible a propósito —hay un flujo legítimo
    de supervisor registrando en nombre de otro operador— pero debe pertenecer
    a la misma empresa que la orden padre; ver ``validate()``.
    """

    #: Nombre de la FK a la orden padre en el modelo concreto.
    orden_padre_field = None

    def validate(self, attrs):
        """Dos candados de tenant, en orden: la orden padre contra el
        solicitante, y ``usuario`` contra la orden padre.

        1) La orden padre (``ob``/``orden_r``) debe pertenecer a la empresa
           de ``request.user``. Sólo aplica en **creación**: la FK es
           write-once (ver ``update()`` abajo), así que en update ya es
           estructuralmente imposible reasignarla a otro tenant —revalidarla
           ahí sería además un falso rechazo, porque el cliente podría
           reenviar por error un valor que ``update()`` va a descartar de
           todas formas sin que afecte nada—.

           Este es el gap que dejó abierto la sesión anterior: ``create()``
           no pasa por ``get_queryset()`` (eso sólo filtra list/retrieve/
           update/destroy vía ``get_object()``), así que sin este chequeo un
           usuario de la empresa A podía crear un avance/incidencia apuntando
           a una orden de la empresa B. Mismo criterio y mismo mensaje que
           ``OrdenReflejanteService._validar_contexto`` para pedido vs.
           empresa del solicitante —incluido el mismo tratamiento sin
           excepción de superuser: ahí el chequeo de empresa corre
           incondicional (sólo la sucursal tiene bypass de ``es_staff``,
           fuera de alcance aquí), y aquí se replica igual: un superuser sin
           ``empresa`` asignada también queda bloqueado por este path
           —tiene la vía de Django admin si necesita alcance cross-tenant—.

        2) ``usuario`` (autoría) debe pertenecer a la empresa de la orden
           padre. No tiene por qué ser ``request.user`` (flujo de supervisor
           registrando en nombre de otro operador, ya confirmado legítimo),
           pero si pertenece a otra empresa que la orden, el registro queda
           inconsistente —un empleado de la empresa B firmando trabajo de una
           orden de la empresa A—. Se ancla contra la orden padre —no contra
           ``request.user.empresa``— porque es lo único que garantiza esa
           consistencia interna usuario↔orden incluso si (1) fallara por
           algún motivo no cubierto. En update, la orden se lee siempre de
           ``self.instance`` (inmutable), nunca de lo que el cliente haya
           reenviado. Tampoco tiene excepción de superuser: es consistencia
           interna entre dos valores del propio payload, no una pregunta de
           alcance del solicitante.

        Si (1) y (2) fallarían ambos, sólo se ve el error de (1): se
        cortocircuita ahí y no se evalúa (2) — un solo error limpio, no una
        combinación confusa.
        """
        attrs = super().validate(attrs)

        if self.instance is None:
            orden = attrs.get(self.orden_padre_field)
            if orden is not None:
                request = self.context.get('request')
                user = getattr(request, 'user', None) if request else None
                # Mismo núcleo de pertenencia a empresa que
                # ``_validar_contexto`` de los services (ver
                # ``produccion.services.common.revisar_empresa``); aquí con la
                # convención de error del serializer (dict por campo).
                resultado = revisar_empresa(user, orden)
                if resultado == 'sin_empresa':
                    raise serializers.ValidationError(
                        {self.orden_padre_field: 'El usuario no tiene una empresa asignada.'}
                    )
                if resultado == 'otra_empresa':
                    raise serializers.ValidationError(
                        {self.orden_padre_field: 'La orden no pertenece a la empresa del usuario.'}
                    )
                # Mismo alcance que la lectura (``_OrdenPadreTenantScopedMixin``):
                # un no-admin sólo escribe en OBs de sus sucursales permitidas.
                if (
                    not getattr(user, 'is_admin_empresa', False)
                    and orden.sucursal_id not in user.sucursales_permitidas()
                ):
                    raise serializers.ValidationError(
                        {self.orden_padre_field: 'La orden no pertenece a una sucursal del usuario.'}
                    )

        if 'usuario' not in attrs:
            return attrs

        usuario = attrs['usuario']
        orden = (
            getattr(self.instance, self.orden_padre_field)
            if self.instance is not None
            else attrs.get(self.orden_padre_field)
        )

        if orden is not None and usuario.empresa_id != orden.empresa_id:
            raise serializers.ValidationError(
                {'usuario': 'El usuario asignado no pertenece a la empresa de la orden.'}
            )

        return attrs

    def update(self, instance, validated_data):
        validated_data.pop(self.orden_padre_field, None)
        return super().update(instance, validated_data)


class BordadoAvancesSerializer(_OrdenPadreWriteOnceMixin, serializers.ModelSerializer):
    orden_padre_field = 'ob'

    orden_bordado_detalle_display = serializers.SerializerMethodField()
    pedido_detalle_talla_display = serializers.SerializerMethodField()

    class Meta:
        model = BordadoAvances
        fields = '__all__'
        # ``usuario`` es read_only: la autoría la inyecta siempre
        # ``perform_create`` con ``request.user`` (ver ViewSet), así que exigirlo
        # en el body sólo producía un 400 espurio. El chequeo usuario↔empresa del
        # ``_OrdenPadreWriteOnceMixin`` queda inerte aquí (usuario nunca llega en
        # ``attrs``) y es correcto: request.user siempre es de su propia empresa.
        # Captura simple por OB: ya no se captura por talla/SKU. El detalle y
        # la talla quedan solo lectura (registros históricos) y
        # ``puntadas_total`` lo calcula el backend (puntadas_por_pieza × piezas).
        read_only_fields = [
            'activo', 'usuario', 'orden_bordado_detalle',
            'pedido_detalle_talla', 'puntadas_total',
        ]
        extra_kwargs = {
            'puntadas_por_pieza': {'required': True, 'min_value': 0},
            'cantidad_bordada': {'min_value': 0},
        }

    def get_orden_bordado_detalle_display(self, obj):
        det = getattr(obj, "orden_bordado_detalle", None)
        if not det:
            return None
        producto = getattr(det, "producto", None)
        talla = getattr(det, "talla", None)
        color = getattr(det, "color", None)
        return {
            "id": det.pk,
            "producto_id": getattr(det, "producto_id", None),
            "producto_nombre": getattr(producto, "nombre", None),
            "talla_id": getattr(det, "talla_id", None),
            "talla_nombre": getattr(talla, "nombre", None) if talla else None,
            "color_id": getattr(det, "color_id", None),
            "color_nombre": getattr(color, "nombre", None) if color else None,
            "cantidad_programada": getattr(det, "cantidad", None),
            "posicion_bordado": getattr(det, "posicion_bordado", None),
        }

    def get_pedido_detalle_talla_display(self, obj):
        pdt = getattr(obj, "pedido_detalle_talla", None)
        if not pdt:
            return None
        talla = getattr(pdt, "talla", None)
        return {
            "id": pdt.pk,
            "pedido_detalle_id": getattr(pdt, "pedido_detalle_id", None),
            "talla_id": getattr(pdt, "talla_id", None),
            "talla_nombre": getattr(talla, "nombre", None) if talla else None,
            "cantidad_pedido": getattr(pdt, "cantidad", None),
        }

class BordadoIncidenciasSerializer(_OrdenPadreWriteOnceMixin, serializers.ModelSerializer):
    orden_padre_field = 'ob'

    class Meta:
        model = BordadoIncidencias
        fields = '__all__'
        read_only_fields = ['activo']

class OrdenReflejanteDetalleSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)
    reflejante_config = serializers.SerializerMethodField()
    ubicaciones = serializers.SerializerMethodField()
    foto = serializers.SerializerMethodField()
    notas = serializers.SerializerMethodField()

    class Meta:
        model = OrdenReflejanteDetalle
        fields = '__all__'

    def _get_pedido_detalle_talla(self, obj):
        """La ``PedidoDetalleTalla`` de origen de este renglón.

        De ella sólo se lee ``reflejante_config`` (en ``_get_cfg``), así que
        basta con un portador de ese atributo.

        ``context["pdt_reflejante_config_map"]`` —cuando existe— es
        ``{(pedido_detalle_id, talla_id): reflejante_config}`` para TODO el
        pedido, resuelto en UNA query por ``OrdenReflejanteViewSet.retrieve()``.
        Sin él este método consultaba una fila por renglón:
        ``_pdt_reflejante_cache`` comparte instancia entre filas (DRF reusa el
        serializer hijo con ``many=True``), pero la clave
        ``(pedido_detalle_id, talla_id)`` es única por renglón por
        construcción, así que nunca acertaba. Mismo arreglo que en
        ``OrdenBordadoDetalleSerializer``.

        La query por fila se conserva como fallback para cualquier llamador que
        no inyecte el mapa (``create``, anidaciones, comandos).
        """
        if obj.pedido_detalle_id is None or obj.talla_id is None:
            return None
        if not hasattr(self, '_pdt_reflejante_cache'):
            self._pdt_reflejante_cache = {}
        key = (obj.pedido_detalle_id, obj.talla_id)
        if key not in self._pdt_reflejante_cache:
            mapa = self.context.get("pdt_reflejante_config_map")
            if mapa is not None:
                # Ausente en el mapa == no existe la fila: mismo ``None`` que
                # devolvía ``.first()``.
                self._pdt_reflejante_cache[key] = (
                    SimpleNamespace(reflejante_config=mapa[key])
                    if key in mapa
                    else None
                )
            else:
                from ventas.models import PedidoDetalleTalla
                self._pdt_reflejante_cache[key] = (
                    PedidoDetalleTalla.objects
                    .filter(pedido_detalle_id=obj.pedido_detalle_id, talla_id=obj.talla_id)
                    .only('reflejante_config')
                    .first()
                )
        return self._pdt_reflejante_cache[key]

    def _get_cfg(self, obj):
        """El ``reflejante_config`` CRUDO, tal cual lo guardó ventas.

        Aquí no se normaliza nada —ni siquiera el ``or {}`` que tenía antes—:
        es lo que viaja en el campo ``reflejante_config`` de la respuesta, y en
        datos reales es SIEMPRE un arreglo (ver ``_get_cfg_dict``).

        Los dos consumidores absorben el ``None`` por su cuenta, así que aquel
        ``or {}`` era pura redundancia: ``get_reflejante_config`` hace
        ``cfg or None`` (``{}`` y ``None`` dan lo mismo) y ``_get_cfg_dict``
        pasa por ``config_como_dict``, que ya mapea cualquier no-dict a ``{}``.
        """
        pdt = self._get_pedido_detalle_talla(obj)
        return getattr(pdt, 'reflejante_config', None)

    def _get_cfg_dict(self, obj):
        """El config sólo si es un dict; ``{}`` en cualquier otro caso.

        ``reflejante_config`` NO tiene la forma de ``bordado_config``: en los
        37 registros que lo traen es un ARREGLO de un elemento
        ``[{"tipo": …, "opcion": …, "posicion": …}]`` —nunca un dict—, así que
        ``cfg.get('ubicaciones')`` reventaba con ``AttributeError: 'list'
        object has no attribute 'get'`` y el ``retrieve`` respondía 500.

        Este helper existe para que las tres extracciones de abajo —copiadas
        de Bordado, donde el config SÍ es un dict— no exploten. No traduce el
        arreglo a ``ubicaciones``: no es lo mismo. Los elementos de bordado
        describen el estampado (``codigo``, ``imagen``, medidas, técnicas) y
        los de reflejante describen el material y dónde va (``tipo``,
        ``opcion``, ``posicion``); mapear unos a otros devolvería datos
        plausibles pero equivocados. ``ubicaciones``/``foto``/``notas`` quedan
        vacíos porque para reflejante NO existe ese dato: ninguna fila
        menciona ``ubicaciones``, ``foto``, ``imagen`` ni ``notas``.

        El arreglo no se pierde: viaja íntegro en ``reflejante_config``, que
        sigue leyendo el valor crudo con ``_get_cfg``.

        La normalización vive en ``services.common.config_como_dict``: este
        mismo criterio lo necesita también el GET de onboarding
        (``_payload_pedidos_onboarding``), y tenerlo duplicado fue justo lo que
        dejó ese endpoint sin guardia.
        """
        return config_como_dict(self._get_cfg(obj))

    def get_reflejante_config(self, obj):
        cfg = self._get_cfg(obj)
        return cfg or None

    def get_ubicaciones(self, obj):
        cfg = self._get_cfg_dict(obj)
        ubicaciones = cfg.get('ubicaciones')
        return ubicaciones if isinstance(ubicaciones, list) else []

    def get_foto(self, obj):
        cfg = self._get_cfg_dict(obj)
        for key in ('foto', 'imagen', 'imagen_url', 'foto_url'):
            value = cfg.get(key)
            if value:
                if isinstance(value, dict):
                    return value
                return {'url': value}
        return None

    def get_notas(self, obj):
        cfg = self._get_cfg_dict(obj)
        for key in ('notas', 'observaciones', 'comentarios'):
            value = cfg.get(key)
            if value:
                return value
        return None

class OrdenReflejanteSerializer(serializers.ModelSerializer):
    detalles = OrdenReflejanteDetalleSerializer(many=True, read_only=True)
    pedido_folio = serializers.CharField(source='pedido.folio', read_only=True)
    # ``empresa``/``sucursal`` siguen viajando como id (read_only en Meta); estos
    # dos son etiquetas legibles adicionales, mismo patrón ``source=`` que
    # ``pedido_folio`` y que ``sucursal_nombre`` en compras/wms. ``Empresa`` no
    # tiene campo ``nombre``: su nombre humano es ``razon_social``.
    empresa_nombre = serializers.CharField(source='empresa.razon_social', read_only=True)
    sucursal_nombre = serializers.CharField(source='sucursal.nombre', read_only=True)
    usuario_nombre = serializers.SerializerMethodField()
    estatus_reflejante_display = serializers.CharField(source="get_estatus_reflejante_display", read_only=True)

    detalles_override = serializers.ListField(
        child=serializers.JSONField(),
        required=False,
        write_only=True,
        allow_null=True,
    )

    class Meta:
        model = OrdenesReflejante
        fields = '__all__'
        read_only_fields = [
            'empresa',
            'sucursal',
            'folio_reflejante',
            'usuario_asignado',
            'activo'
        ]

    def get_usuario_nombre(self, obj):
        usuario = obj.usuario_asignado
        if not usuario: return None
        return usuario.get_full_name().strip() or usuario.email

    def validate_pedido(self, pedido):
        return _pedido_en_alcance(self, pedido, "reflejante", "REFLEJANTE")

    def validate(self, attrs):
        detalles_override = attrs.get("detalles_override") or []
        pedido = attrs.get("pedido")
        if detalles_override:
            from ventas.models import PedidoDetalleTalla

            seen_ids = set()
            for item in detalles_override:
                if not isinstance(item, dict):
                    raise serializers.ValidationError({
                        "detalles_override": "Cada renglón debe ser un objeto."
                    })
                pdt_id = item.get("pedido_detalle_talla_id")
                cantidad = item.get("cantidad")
                if pdt_id is None:
                    raise serializers.ValidationError({
                        "detalles_override": "Falta `pedido_detalle_talla_id`."
                    })
                if pdt_id in seen_ids:
                    raise serializers.ValidationError({
                        "detalles_override": f"`pedido_detalle_talla_id={pdt_id}` repetido."
                    })
                seen_ids.add(pdt_id)
                try:
                    cantidad_num = float(cantidad)
                except (TypeError, ValueError):
                    raise serializers.ValidationError({
                        "detalles_override": f"`cantidad` inválida para `pedido_detalle_talla_id={pdt_id}`."
                    })
                if cantidad_num <= 0:
                    raise serializers.ValidationError({
                        "detalles_override": f"`cantidad` debe ser mayor a 0 para `pedido_detalle_talla_id={pdt_id}`."
                    })
                # ``PedidoDetalleTalla.cantidad`` es ``PositiveIntegerField``:
                # las prendas se reflejan enteras. Aceptar fraccionarios metía
                # residuos de coma flotante en las sumas de cupo y dejaba
                # pendientes de 1e-15 que ninguna OR podía consumir. Mismo
                # criterio y mismo mensaje que ``OrdenBordadoSerializer``.
                if cantidad_num != int(cantidad_num):
                    raise serializers.ValidationError({
                        "detalles_override": (
                            f"`cantidad` debe ser un número entero de piezas para "
                            f"`pedido_detalle_talla_id={pdt_id}` (llegó {cantidad_num})."
                        )
                    })
                if pedido is not None:
                    # Filtrado por el pedido: un id de otro pedido responde igual que uno
                    # inexistente y no deja enumerar tallas ajenas.
                    try:
                        pdt = PedidoDetalleTalla.objects.select_related("pedido_detalle").get(
                            pk=pdt_id, pedido_detalle__pedido=pedido
                        )
                    except PedidoDetalleTalla.DoesNotExist:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no existe "
                                f"en el pedido `{pedido.pk}`."
                            )
                        })
                    if not pdt.lleva_reflejante:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no lleva "
                                "servicio de reflejante (`lleva_reflejante=False`)."
                            )
                        })
                    if cantidad_num > float(pdt.cantidad or 0):
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}`: la cantidad "
                                f"{cantidad_num} excede la del pedido "
                                f"({float(pdt.cantidad or 0)})."
                            )
                        })
        return attrs
        
class OrdenReflejanteDetalleListSerializer(serializers.ModelSerializer):
    """Renglón de orden de reflejante para el listado."""

    # NOTA (comentario, no docstring: drf-spectacular lo publicaría como
    # `description` del componente en /api/schema/ y /api/docs/).
    #
    # Mismo motivo que en Bordado (ver ``OrdenBordadoDetalleListSerializer``):
    # el serializer completo dispara una query a ``PedidoDetalleTalla`` por
    # renglón. Al no declarar los campos derivados de ``reflejante_config``,
    # el listado tampoco ejecuta la extracción que los resuelve.

    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)

    class Meta:
        model = OrdenReflejanteDetalle
        # Mismo motivo que en ``OrdenBordadoDetalleListSerializer``: sin este
        # ``exclude``, ``__all__`` publicaría el ``reflejante_config`` entero
        # en cada renglón del listado.
        exclude = ('configuracion',)


class OrdenReflejanteListSerializer(_ResumenAvancePiezasListadoMixin, OrdenReflejanteSerializer):
    """Orden de reflejante para el listado."""

    # Hereda de ``OrdenReflejanteSerializer`` para que el encabezado no pueda
    # divergir: mismos campos, mismo ``Meta``, mismo ``usuario_nombre``. Lo
    # único que cambia es que ``detalles`` usa el renglón ligero y que se
    # añaden los tres campos de cobertura.
    #
    # La cobertura NO se calcula aquí por fila: el ``ViewSet`` la resuelve para
    # todo el conjunto en 2 queries agrupadas y la deja en el contexto bajo
    # ``cobertura`` (ver ``OrdenReflejanteService.cobertura_por_orden``). Si
    # cada fila la calculara sola volveríamos al N+1 que este listado ya se
    # quitó de encima. Mismo patrón que ``OrdenBordadoListSerializer``.

    detalles = OrdenReflejanteDetalleListSerializer(many=True, read_only=True)

    cantidad_cubierta = serializers.SerializerMethodField()
    cantidad_contratada = serializers.SerializerMethodField()
    cobertura_completa = serializers.SerializerMethodField()

    def _cobertura(self, obj):
        # Se exige la clave, no se asume: sin ella los tres getters devolverían
        # 0/0/False —una respuesta bien formada afirmando que la orden no cubre
        # nada de nada—, indistinguible de un dato real. Cualquier uso de este
        # serializer fuera de ``OrdenReflejanteViewSet.list``/``retrieve`` (una
        # anidación, una acción nueva, un comando) publicaría ceros como hechos.
        # Se prefiere fallar de forma visible. ``.get(obj.pk)`` sí puede faltar
        # legítimamente —una orden ajena al conjunto— y ahí el default es
        # correcto.
        if "cobertura" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'cobertura' en el contexto; "
                "lo inyecta OrdenReflejanteViewSet (ver cobertura_por_orden)."
            )
        return (self.context["cobertura"] or {}).get(obj.pk) or {}

    def get_cantidad_cubierta(self, obj):
        """Piezas que programa ESTA orden."""
        return self._cobertura(obj).get("cubierto", 0)

    def get_cantidad_contratada(self, obj):
        """Piezas de reflejante que contrató el pedido (todas sus líneas)."""
        return self._cobertura(obj).get("contratado", 0)

    def get_cobertura_completa(self, obj):
        """¿Esta orden sola cubre el 100% de lo contratado por el pedido?"""
        return self._cobertura(obj).get("completa", False)


class OrdenReflejanteDetalleRetrieveSerializer(_SkuPorTallaMixin, OrdenReflejanteDetalleSerializer):
    """Renglón del DETALLE, con el contexto de parcialidad de su línea."""

    # Mismos nombres que ya usa el GET de onboarding para estos tres conceptos
    # (``cantidad_pedido``/``cantidad_asignada``/``cantidad_pendiente``), para
    # no inventar un vocabulario paralelo. Aquí ``cantidad`` sigue siendo lo que
    # programa ESTA orden; los tres nuevos hablan del pedido y de TODAS sus ORs
    # activas.
    #
    # El ``ViewSet`` deja el mapa por línea en el contexto: una sola resolución
    # para todos los renglones, sin query por fila.

    cantidad_pedido = serializers.SerializerMethodField()
    cantidad_asignada = serializers.SerializerMethodField()
    cantidad_pendiente = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()

    def _linea(self, obj):
        """``(pedido, asignada, pendiente)`` de este renglón.

        Se exige la clave del contexto por el mismo motivo que
        ``OrdenReflejanteListSerializer._cobertura``: sin ella los tres getters
        devolverían ``null`` en silencio.

        Un renglón con ``talla`` NULL —los genera el pipeline de picking, y en
        reflejante además los renglones históricos anteriores al arreglo del
        ``reflejante_config``— no tiene entrada por talla, porque el mapa se
        construye desde ``PedidoDetalleTalla``, que siempre la trae. Eso daría
        tres ``null`` justo en el caso que ``reparto_por_talla_aproximado`` dice
        estar describiendo: la bandera anunciaría "reparto aproximado" y el
        renglón no mostraría número alguno. Se cae al total del
        ``pedido_detalle``, que sí es exacto (es lo que garantiza
        ``pendientes_por_linea``), de modo que la bandera y los números hablan
        de lo mismo.
        """
        if "partialidad_por_linea" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'partialidad_por_linea' en el "
                "contexto; lo inyecta OrdenReflejanteViewSet.retrieve "
                "(ver partialidad_de_orden)."
            )
        mapa = self.context["partialidad_por_linea"] or {}
        linea = mapa.get((obj.pedido_detalle_id, obj.talla_id))
        if linea is not None:
            return linea
        por_detalle = self.context.get("partialidad_por_detalle") or {}
        return por_detalle.get(obj.pedido_detalle_id)

    def get_cantidad_pedido(self, obj):
        """Piezas contratadas por el pedido en esta línea.

        Para un renglón sin talla identificable, el total del ``pedido_detalle``.
        """
        linea = self._linea(obj)
        return linea[0] if linea else None

    def get_cantidad_asignada(self, obj):
        """Piezas ya programadas en esta línea por TODAS las ORs activas."""
        linea = self._linea(obj)
        return linea[1] if linea else None

    def get_cantidad_pendiente(self, obj):
        """Saldo de la línea: ``cantidad_pedido - cantidad_asignada``."""
        linea = self._linea(obj)
        return linea[2] if linea else None


class OrdenReflejanteHermanaSerializer(serializers.Serializer):
    """Referencia mínima a otra OR activa del mismo pedido.

    Existe para que ``fecha_inicio`` pase por un ``DateTimeField`` de DRF. El
    servicio la trae con ``.values(...)``, así que llegaría como ``datetime``
    crudo y el renderer JSON la emitiría en un formato distinto (UTC, sufijo
    ``Z``) al de la ``fecha_inicio`` de la propia orden, que sí es campo de
    modelo y respeta ``DATETIME_FORMAT`` y la zona activa. Dos formas distintas
    del mismo instante, bajo el mismo nombre, en una sola respuesta.
    """

    id = serializers.IntegerField(read_only=True)
    folio_reflejante = serializers.CharField(read_only=True)
    fecha_inicio = serializers.DateTimeField(read_only=True)


class OrdenReflejanteRetrieveSerializer(OrdenReflejanteListSerializer):
    """Orden de reflejante para el detalle, con contexto de parcialidad.

    Hereda de ``OrdenReflejanteListSerializer`` —no de
    ``OrdenReflejanteSerializer``— para conservar los tres campos de cobertura
    (``cantidad_cubierta``/``cantidad_contratada``/``cobertura_completa``). Sin
    ellos el detalle no podría enunciar "cubre 7 de 40": los campos por línea no
    sustituyen al total, porque el detalle sólo itemiza las líneas que ESTA
    orden toca. Un diálogo montado sobre el GET por id tendría que leer además
    la fila del listado, que es justo lo que el fetch por id viene a evitar.

    ``create`` conserva ``OrdenReflejanteSerializer`` tal cual, así que el alta
    no cambia lo que devuelve.
    """

    # Vuelve a apuntar al renglón completo: ``OrdenReflejanteListSerializer`` lo
    # deja en el ligero (sin ``ubicaciones``/``foto``/``notas``/
    # ``reflejante_config``), que es correcto para el listado pero no para el
    # detalle.
    detalles = OrdenReflejanteDetalleRetrieveSerializer(many=True, read_only=True)

    otras_ordenes_del_pedido = serializers.SerializerMethodField()
    reparto_por_talla_aproximado = serializers.SerializerMethodField()
    pedido_vinculado = serializers.SerializerMethodField()
    avances = serializers.SerializerMethodField()
    resumen_avance = serializers.SerializerMethodField()

    def get_avances(self, obj):
        return AvancePiezasReadSerializer(self.context.get("avances") or [], many=True).data

    def get_resumen_avance(self, obj):
        return resumen_avance_piezas(obj.detalles.all(), self.context.get("avances") or [])

    def get_pedido_vinculado(self, obj):
        from produccion.services.orden_bordado_field_filter_service import armar_pedido_vinculado
        return armar_pedido_vinculado(obj)

    def get_otras_ordenes_del_pedido(self, obj):
        """Las demás ORs activas del mismo pedido, sin incluir ésta."""
        # Se exige la clave: un ``[]`` por contexto ausente es indistinguible de
        # una orden que de verdad no tiene hermanas. Ver ``_cobertura``.
        if "hermanas" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'hermanas' en el contexto; "
                "lo inyecta OrdenReflejanteViewSet.retrieve "
                "(ver partialidad_de_orden)."
            )
        return OrdenReflejanteHermanaSerializer(
            self.context["hermanas"] or [], many=True
        ).data

    def get_reparto_por_talla_aproximado(self, obj):
        """¿El reparto por talla es aproximado?

        ``True`` cuando el pedido tiene piezas programadas sin talla
        identificable (renglones con ``talla`` NULL, que genera el pipeline de
        picking). El total por ``pedido_detalle`` sigue siendo exacto; lo que no
        se puede afirmar es a qué talla concreta pertenecen.
        """
        return bool(self.context.get("reparto_aproximado", False))


class ReflejanteAvancesSerializer(_OrdenPadreWriteOnceMixin, _AvancePiezasWriteSerializer):
    """Body ``{orden_r, cantidad, comentario?}``. ``cantidad`` es la columna
    ``cantidad_aplicada``; el usuario sale de la sesión."""

    orden_padre_field = 'orden_r'
    cantidad = serializers.FloatField(source='cantidad_aplicada')

    class Meta:
        model = ReflejanteAvances
        fields = ['id', 'orden_r', 'fecha', 'cantidad', 'comentario', 'usuario', 'usuario_nombre']
        read_only_fields = ['id', 'fecha', 'usuario']


class CorteMangaAvancesSerializer(_OrdenPadreWriteOnceMixin, _AvancePiezasWriteSerializer):
    """Body ``{ocm, cantidad, comentario?}``; el usuario sale de la sesión."""

    orden_padre_field = 'ocm'

    class Meta:
        model = CorteMangaAvances
        fields = ['id', 'ocm', 'fecha', 'cantidad', 'comentario', 'usuario', 'usuario_nombre']
        read_only_fields = ['id', 'fecha', 'usuario']

class ReflejanteIncidenciasSerializer(_OrdenPadreWriteOnceMixin, serializers.ModelSerializer):
    orden_padre_field = 'orden_r'

    class Meta:
        model = ReflejanteIncidencias
        fields = '__all__'
        read_only_fields = ['activo']

class OrdenCorteMangaDetalleSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)
    corte_manga_config = serializers.SerializerMethodField()
    ubicaciones = serializers.SerializerMethodField()
    foto = serializers.SerializerMethodField()
    notas = serializers.SerializerMethodField()

    class Meta:
        model = OrdenCorteMangaDetalle
        fields = '__all__'

    def _get_pedido_detalle_talla(self, obj):
        """La ``PedidoDetalleTalla`` de origen de este renglón.

        De ella sólo se lee ``corte_manga_config`` (en ``_get_cfg``), así que
        basta con un portador de ese atributo. Ojo: aquí ese config es apenas
        la BASE de la mezcla —``get_corte_manga_config`` le encima
        ``obj.configuracion``, columna del propio renglón—, y esa lógica no se
        toca: lo único que cambia es de dónde sale ``cfg_pedido``.

        ``context["pdt_corte_manga_config_map"]`` —cuando existe— es
        ``{(pedido_detalle_id, talla_id): corte_manga_config}`` para TODO el
        pedido, resuelto en UNA query por ``OrdenesCorteMangaViewSet.retrieve()``.
        Sin él este método consultaba una fila por renglón: ``_pdt_ocm_cache``
        comparte instancia entre filas (DRF reusa el serializer hijo con
        ``many=True``), pero la clave ``(pedido_detalle_id, talla_id)`` es única
        por renglón por construcción, así que nunca acertaba. Mismo arreglo que
        en ``OrdenBordadoDetalleSerializer``/``OrdenReflejanteDetalleSerializer``,
        con clave de contexto propia para que los tres mapas no se pisen.

        La query por fila se conserva como fallback para cualquier llamador que
        no inyecte el mapa (``create``, anidaciones, comandos).
        """
        if obj.pedido_detalle_id is None or obj.talla_id is None:
            return None
        if not hasattr(self, '_pdt_ocm_cache'):
            self._pdt_ocm_cache = {}
        key = (obj.pedido_detalle_id, obj.talla_id)
        if key not in self._pdt_ocm_cache:
            mapa = self.context.get("pdt_corte_manga_config_map")
            if mapa is not None:
                # Ausente en el mapa == no existe la fila: mismo ``None`` que
                # devolvía ``.first()`` (y que ``_get_cfg`` colapsa a ``{}``).
                self._pdt_ocm_cache[key] = (
                    SimpleNamespace(corte_manga_config=mapa[key])
                    if key in mapa
                    else None
                )
            else:
                from ventas.models import PedidoDetalleTalla
                self._pdt_ocm_cache[key] = (
                    PedidoDetalleTalla.objects
                    .filter(pedido_detalle_id=obj.pedido_detalle_id, talla_id=obj.talla_id)
                    .only('corte_manga_config')
                    .first()
                )
        return self._pdt_ocm_cache[key]

    def _get_cfg(self, obj):
        pdt = self._get_pedido_detalle_talla(obj)
        return getattr(pdt, 'corte_manga_config', None) or {}

    def get_corte_manga_config(self, obj):
        # ``config_como_dict`` en la base de la mezcla: ``dict(cfg_pedido)``
        # sobre un config con forma de arreglo NO falla como los ``.get()`` del
        # resto del módulo —lanza ``ValueError: dictionary update sequence
        # element #0 has length 1; 2 is required``, no ``AttributeError``—, así
        # que es la misma clase de defecto con otra cara. Sobre un dict,
        # ``dict(config_como_dict(d))`` es idéntico a ``dict(d)``: no-op para el
        # 100% de las filas de hoy (36/36 objetos).
        #
        # El camino sin ``obj.configuracion`` sigue devolviendo el valor CRUDO:
        # publicar el config íntegro es lo correcto aunque no sea un dict; de
        # normalizarlo se encargan los tres getters de abajo.
        cfg_pedido = self._get_cfg(obj)
        if obj.configuracion:
            merged = dict(config_como_dict(cfg_pedido))
            if isinstance(obj.configuracion, dict):
                merged.update(obj.configuracion)
            return merged or None
        return cfg_pedido or None

    def get_ubicaciones(self, obj):
        cfg = config_como_dict(self.get_corte_manga_config(obj))
        ubicaciones = cfg.get('ubicaciones')
        return ubicaciones if isinstance(ubicaciones, list) else []

    def get_foto(self, obj):
        cfg = config_como_dict(self.get_corte_manga_config(obj))
        for key in ('foto', 'imagen', 'imagen_url', 'foto_url'):
            value = cfg.get(key)
            if value:
                if isinstance(value, dict):
                    return value
                return {'url': value}
        return None

    def get_notas(self, obj):
        cfg = config_como_dict(self.get_corte_manga_config(obj))
        for key in ('notas', 'observaciones', 'comentarios'):
            value = cfg.get(key)
            if value:
                return value
        return None

class OrdenesCorteMangaSerializer(serializers.ModelSerializer):
    pedido_folio = serializers.CharField(source='pedido.folio', read_only=True)
    # ``empresa``/``sucursal`` siguen viajando como id (read_only en Meta); estos
    # dos son etiquetas legibles adicionales, mismo patrón ``source=`` que
    # ``pedido_folio`` (ver ``OrdenReflejanteSerializer``/``OrdenBordadoSerializer``).
    # ``Empresa`` no tiene campo ``nombre``: su nombre humano es ``razon_social``.
    empresa_nombre = serializers.CharField(source='empresa.razon_social', read_only=True)
    sucursal_nombre = serializers.CharField(source='sucursal.nombre', read_only=True)
    usuario_nombre = serializers.SerializerMethodField()
    detalles = OrdenCorteMangaDetalleSerializer(many=True, read_only=True)
    estatus_corte_display = serializers.CharField(source="get_estatus_corte_display", read_only=True)

    detalles_override = serializers.ListField(
        child=serializers.JSONField(),
        required=False,
        write_only=True,
        allow_null=True,
    )

    class Meta:
        model = OrdenesCorteManga
        fields = '__all__'
        read_only_fields = [
            'empresa', 
            'sucursal', 
            'folio_ocm', 
            'estatus_corte', 
            'usuario_asignado', 
            'activo'
        ]

    def get_usuario_nombre(self, obj):
        usuario = obj.usuario_asignado
        if not usuario: return None
        return usuario.get_full_name().strip() or usuario.email

    def validate_pedido(self, pedido):
        return _pedido_en_alcance(self, pedido, "corte de manga", "CORTE_MANGA")

    def validate(self, attrs):
        detalles_override = attrs.get("detalles_override") or []
        pedido = attrs.get("pedido")
        if detalles_override:
            from ventas.models import PedidoDetalleTalla

            seen_ids = set()
            for item in detalles_override:
                if not isinstance(item, dict):
                    raise serializers.ValidationError({
                        "detalles_override": "Cada renglón debe ser un objeto."
                    })
                pdt_id = item.get("pedido_detalle_talla_id")
                cantidad = item.get("cantidad")
                if pdt_id is None:
                    raise serializers.ValidationError({
                        "detalles_override": "Falta `pedido_detalle_talla_id`."
                    })
                if pdt_id in seen_ids:
                    raise serializers.ValidationError({
                        "detalles_override": f"`pedido_detalle_talla_id={pdt_id}` repetido."
                    })
                seen_ids.add(pdt_id)
                try:
                    cantidad_num = float(cantidad)
                except (TypeError, ValueError):
                    raise serializers.ValidationError({
                        "detalles_override": f"`cantidad` inválida para `pedido_detalle_talla_id={pdt_id}`."
                    })
                if cantidad_num <= 0:
                    raise serializers.ValidationError({
                        "detalles_override": f"`cantidad` debe ser mayor a 0 para `pedido_detalle_talla_id={pdt_id}`."
                    })
                # Piezas enteras, mismo criterio y mensaje que OB/OR: los
                # fraccionarios dejaban residuos de coma flotante en el cupo.
                if cantidad_num != int(cantidad_num):
                    raise serializers.ValidationError({
                        "detalles_override": (
                            f"`cantidad` debe ser un número entero de piezas para "
                            f"`pedido_detalle_talla_id={pdt_id}` (llegó {cantidad_num})."
                        )
                    })
                if pedido is not None:
                    # Filtrado por el pedido: un id de otro pedido responde igual que uno
                    # inexistente y no deja enumerar tallas ajenas.
                    try:
                        pdt = PedidoDetalleTalla.objects.select_related("pedido_detalle").get(
                            pk=pdt_id, pedido_detalle__pedido=pedido
                        )
                    except PedidoDetalleTalla.DoesNotExist:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no existe "
                                f"en el pedido `{pedido.pk}`."
                            )
                        })
                    if not pdt.lleva_corte_manga:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no lleva "
                                "servicio de corte de manga (`lleva_corte_manga=False`)."
                            )
                        })
                    if cantidad_num > float(pdt.cantidad or 0):
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}`: la cantidad "
                                f"{cantidad_num} excede la del pedido "
                                f"({float(pdt.cantidad or 0)})."
                            )
                        })
        return attrs


class OrdenCorteMangaDetalleListSerializer(serializers.ModelSerializer):
    """Renglón de orden de corte de manga para el listado."""

    # NOTA (comentario, no docstring: drf-spectacular lo publicaría como
    # `description` del componente en /api/schema/ y /api/docs/).
    #
    # Mismo motivo que en Bordado (ver ``OrdenBordadoDetalleListSerializer``):
    # una query a ``PedidoDetalleTalla`` por renglón.
    #
    # Ojo con la diferencia de este módulo: ``corte_manga_config`` no sale sólo
    # del pedido, es la mezcla de ``PedidoDetalleTalla.corte_manga_config`` con
    # el campo propio ``OrdenCorteMangaDetalle.configuracion``. Lo que se
    # descarta aquí es únicamente la parte que exige ir al pedido; el campo
    # ``configuracion`` es una columna del propio renglón y sigue viajando en el
    # listado (llega con ``fields = '__all__'``, sin query extra).

    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)

    class Meta:
        model = OrdenCorteMangaDetalle
        fields = '__all__'


class OrdenesCorteMangaListSerializer(_ResumenAvancePiezasListadoMixin, OrdenesCorteMangaSerializer):
    """Orden de corte de manga para el listado."""

    # Sólo cambia el anidado de ``detalles``; ver
    # ``OrdenBordadoListSerializer``.

    detalles = OrdenCorteMangaDetalleListSerializer(many=True, read_only=True)


class OrdenCorteMangaDetalleRetrieveSerializer(_SkuPorTallaMixin, OrdenCorteMangaDetalleSerializer):
    """Renglón del DETALLE de corte de manga con la parcialidad de su línea y su
    ``sku``. Mismo contrato que ``OrdenReflejanteDetalleRetrieveSerializer``."""

    cantidad_pedido = serializers.SerializerMethodField()
    cantidad_asignada = serializers.SerializerMethodField()
    cantidad_pendiente = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()

    def _linea(self, obj):
        if "partialidad_por_linea" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'partialidad_por_linea' en el "
                "contexto; lo inyecta OrdenesCorteMangaViewSet.retrieve."
            )
        linea = (self.context["partialidad_por_linea"] or {}).get((obj.pedido_detalle_id, obj.talla_id))
        if linea is not None:
            return linea
        return (self.context.get("partialidad_por_detalle") or {}).get(obj.pedido_detalle_id)

    def get_cantidad_pedido(self, obj):
        linea = self._linea(obj)
        return linea[0] if linea else None

    def get_cantidad_asignada(self, obj):
        linea = self._linea(obj)
        return linea[1] if linea else None

    def get_cantidad_pendiente(self, obj):
        linea = self._linea(obj)
        return linea[2] if linea else None


class OrdenCorteMangaHermanaSerializer(serializers.Serializer):
    """Referencia mínima a otra OCM activa del mismo pedido."""

    id = serializers.IntegerField(read_only=True)
    folio_ocm = serializers.CharField(read_only=True)
    fecha_inicio = serializers.DateTimeField(read_only=True)


class OrdenesCorteMangaRetrieveSerializer(OrdenesCorteMangaSerializer):
    """Orden de corte de manga para el detalle.

    Hereda del serializer base —no del de listado— para conservar el renglón
    completo (``OrdenCorteMangaDetalleSerializer`` con
    ``corte_manga_config``/``ubicaciones``/``foto``/``notas``). Añade
    ``pedido_vinculado`` sólo en el detalle, igual que
    ``OrdenReflejanteRetrieveSerializer``/``OrdenBordadoRetrieveSerializer``.
    """

    detalles = OrdenCorteMangaDetalleRetrieveSerializer(many=True, read_only=True)
    pedido_vinculado = serializers.SerializerMethodField()
    cantidad_cubierta = serializers.SerializerMethodField()
    cantidad_contratada = serializers.SerializerMethodField()
    cobertura_completa = serializers.SerializerMethodField()
    otras_ordenes_del_pedido = serializers.SerializerMethodField()
    reparto_por_talla_aproximado = serializers.SerializerMethodField()
    avances = serializers.SerializerMethodField()
    resumen_avance = serializers.SerializerMethodField()

    def get_pedido_vinculado(self, obj):
        from produccion.services.orden_bordado_field_filter_service import armar_pedido_vinculado
        return armar_pedido_vinculado(obj)

    def _cobertura(self, obj):
        if "cobertura" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'cobertura' en el contexto; "
                "lo inyecta OrdenesCorteMangaViewSet.retrieve."
            )
        return (self.context["cobertura"] or {}).get(obj.pk) or {}

    def get_cantidad_cubierta(self, obj):
        """Piezas que programa ESTA orden."""
        return self._cobertura(obj).get("cubierto", 0)

    def get_cantidad_contratada(self, obj):
        """Piezas de corte de manga que contrató el pedido (todas sus líneas)."""
        return self._cobertura(obj).get("contratado", 0)

    def get_cobertura_completa(self, obj):
        """¿Esta orden sola cubre el 100% de lo contratado por el pedido?"""
        return self._cobertura(obj).get("completa", False)

    def get_otras_ordenes_del_pedido(self, obj):
        """Las demás OCMs activas del mismo pedido, sin incluir ésta."""
        if "hermanas" not in self.context:
            raise KeyError(
                f"{type(self).__name__} requiere 'hermanas' en el contexto; "
                "lo inyecta OrdenesCorteMangaViewSet.retrieve."
            )
        return OrdenCorteMangaHermanaSerializer(self.context["hermanas"] or [], many=True).data

    def get_reparto_por_talla_aproximado(self, obj):
        return bool(self.context.get("reparto_aproximado", False))

    def get_avances(self, obj):
        return AvancePiezasReadSerializer(self.context.get("avances") or [], many=True).data

    def get_resumen_avance(self, obj):
        return resumen_avance_piezas(obj.detalles.all(), self.context.get("avances") or [])


# --- Pedidos con produccion especial (muestras) para el modulo de produccion ---
# Una linea es especial si trae producto_nombre_externo (muestra sin SKU de
# catalogo); la seleccion vive en ``ventas.services.sku_produccion_service.detalles_de_muestra``.
# Aqui solo se serializa lo que la vista ya filtro.

class VarianteProduccionListSerializer(serializers.ModelSerializer):
    """Variantes de producción (SKU ``MP…``) para elegir en el onboarding de OP.

    ``tiene_bom`` viene anotado (``Exists``) en ``VarianteProduccionViewSet``.
    """
    talla_nombre = serializers.CharField(source="talla.nombre", read_only=True, allow_null=True, default=None)
    color_nombre = serializers.CharField(source="color.nombre", read_only=True, allow_null=True, default=None)
    pedido_id = serializers.IntegerField(source="pedido_detalle.pedido_id", read_only=True, allow_null=True, default=None)
    pedido_folio = serializers.CharField(source="pedido_detalle.pedido.folio", read_only=True, allow_null=True, default=None)
    tiene_bom = serializers.BooleanField(read_only=True)

    class Meta:
        model = VarianteProductoProduccion
        fields = [
            "id", "sku", "nombre", "talla_nombre", "color_nombre",
            "pedido_detalle", "pedido_id", "pedido_folio", "aplica_catalogo", "tiene_bom",
        ]


def ordenes_servicio_pedido(pedido):
    """``{bordado, reflejante, corte_manga}``: órdenes del pedido con
    ``id, folio, estatus, estatus_display, fecha_compromiso, asignado`` (y
    ``proveedor`` en bordado). Lee lo anotado en la vista; sin consultas."""
    from produccion.models import OrdenesBordado, OrdenesCorteManga, OrdenesReflejante
    from ventas.services.clasificacion_service import rango_fecha_entrega
    compromiso = rango_fecha_entrega(pedido)[1]
    tipos = (
        ("bordado", "bordado_json", OrdenesBordado.EstatusBordado),
        ("reflejante", "reflejante_json", OrdenesReflejante.EstatusReflejante),
        ("corte_manga", "corte_manga_json", OrdenesCorteManga.EstatusCorte),
    )
    out = {}
    for clave, attr, choices in tipos:
        etiquetas = dict(choices.choices)
        out[clave] = [
            {**o, "estatus_display": etiquetas.get(o.get("estatus"), ""), "fecha_compromiso": compromiso,
             "estatus_previo_display": etiquetas.get(o.get("estatus_previo")) if o.get("estatus_previo") else None,
             "asignado": o.get("asignado") or None}
            for o in (getattr(pedido, attr, None) or [])
        ]
    return out


def resumen_pre_orden_pedido(pedido):
    """``pre_orden {id, estatus, estatus_display, semaforo, fecha_estimada_material}``
    del pedido, a partir de ``pedido.pre_orden_json`` (anotado en la vista)."""
    from datetime import date
    from produccion.services.pre_orden_service import fecha_compromiso, semaforo
    p = getattr(pedido, "pre_orden_json", None)
    if not p:
        return None
    fem = p.get("fecha_estimada_material")
    fem = date.fromisoformat(fem) if isinstance(fem, str) else fem
    return {
        "id": p["id"],
        "estatus": p["estatus"],
        "estatus_display": dict(PreOrdenProduccion.Estatus.choices).get(p["estatus"]),
        "semaforo": semaforo(p["estatus"], p.get("materiales_faltantes") or 0, fem, fecha_compromiso(pedido)),
        "fecha_estimada_material": fem,
    }


#: Horas desde la confirmación sin pre-orden ni OP para marcar un pedido especial atrasado.
HORAS_ALERTA_PEDIDO_ESPECIAL = 24


class PedidoEspecialListSerializer(serializers.ModelSerializer):
    """Listado ligero: solo lo necesario para que produccion elija cual abrir."""

    class Meta:
        model = Pedido
        fields = [
            "id", "folio", "cliente_nombre", "clasificacion", "fecha_confirmacion",
            # El listado incluye pedidos dados de baja (EC-873): ``activo`` los distingue.
            "activo",
            # Anotados en ``PedidoEspecialViewSet.get_queryset`` (subconsultas):
            # tallas con cantidad > 0 de las líneas de muestra.
            "tallas_total", "tallas_con_sku", "tallas_en_catalogo",
            "fecha_compromiso", "prendas_total", "piezas_total", "servicios",
            "ordenes_produccion", "pre_orden", "ordenes_servicio",
            "horas_en_produccion", "sin_avance", "atrasado",
        ]

    pre_orden = serializers.SerializerMethodField()
    # Alerta de 24 h: horas desde que se creó el pedido,
    # si aún no tiene pre-orden (BOM) ni OP, y si ya pasó el límite.
    horas_en_produccion = serializers.SerializerMethodField()
    sin_avance = serializers.SerializerMethodField()
    atrasado = serializers.SerializerMethodField()

    def get_horas_en_produccion(self, obj):
        from django.utils import timezone
        # ``fecha_confirmacion`` es la fecha comprometida (puede ser futura): se
        # cuenta desde que se creó el pedido.
        if not obj.created_at:
            return None
        return int((timezone.now() - obj.created_at).total_seconds() // 3600)

    def get_sin_avance(self, obj):
        return not getattr(obj, "pre_orden_json", None) and not (getattr(obj, "ops_json", None) or [])

    def get_atrasado(self, obj):
        # Un pedido dado de baja o cancelado sigue en el listado pero nunca está
        # atrasado (EC-873).
        if not obj.activo or obj.estatus == Pedido.ESTATUS_CANCELADO:
            return False
        horas = self.get_horas_en_produccion(obj)
        return horas is not None and horas >= HORAS_ALERTA_PEDIDO_ESPECIAL and self.get_sin_avance(obj)
    ordenes_servicio = serializers.SerializerMethodField()

    def get_pre_orden(self, obj):
        return resumen_pre_orden_pedido(obj)

    def get_ordenes_servicio(self, obj):
        return ordenes_servicio_pedido(obj)

    tallas_total = serializers.IntegerField(read_only=True)
    tallas_con_sku = serializers.IntegerField(read_only=True)
    tallas_en_catalogo = serializers.IntegerField(read_only=True)
    prendas_total = serializers.IntegerField(read_only=True)
    piezas_total = serializers.FloatField(read_only=True)
    fecha_compromiso = serializers.SerializerMethodField()
    servicios = serializers.SerializerMethodField()
    ordenes_produccion = serializers.SerializerMethodField()

    def get_fecha_compromiso(self, obj):
        # Límite del rango de la clasificación desde la confirmación (el mismo
        # ``fecha_entrega_max`` que muestra ventas); ``X`` no tiene compromiso.
        from ventas.services.clasificacion_service import rango_fecha_entrega
        return rango_fecha_entrega(obj)[1]

    def get_servicios(self, obj):
        return {
            "bordado": bool(obj.lleva_bordado_any),
            "reflejante": bool(obj.lleva_reflejante_any),
            "corte_manga": bool(obj.lleva_corte_manga_any),
        }

    def get_ordenes_produccion(self, obj):
        etiquetas = dict(OrdenProduccion.EstatusOrdenProduccion.choices)
        return [
            {**op, "estatus_op_display": etiquetas.get(op["estatus_op"])}
            for op in (obj.ops_json or [])
        ]


class PedidoDetalleTallaEspecialSerializer(serializers.ModelSerializer):
    talla_nombre = serializers.CharField(source="talla.nombre", read_only=True)
    # Llenado en la vista (``PedidoEspecialViewSet.retrieve``) como atributo
    # ``variante_produccion`` -- evita un query por talla para saber si ya
    # tiene SKU de producción generado.
    sku_produccion = serializers.SerializerMethodField()
    sku_siguiente = serializers.SerializerMethodField()
    aplica_catalogo = serializers.SerializerMethodField()
    sku_catalogo = serializers.SerializerMethodField()
    # Llenado en ``PedidoEspecialViewSet.retrieve`` (``_avance_por_talla_especial``).
    avance = serializers.SerializerMethodField()

    def get_avance(self, obj):
        return getattr(obj, "avance", None)

    def get_aplica_catalogo(self, obj):
        return bool(getattr(getattr(obj, "variante_produccion", None), "aplica_catalogo", False))

    def get_sku_catalogo(self, obj):
        return getattr(getattr(obj, "variante_produccion", None), "sku_catalogo", None)

    def get_sku_produccion(self, obj):
        variante = getattr(obj, "variante_produccion", None)
        # ``sku`` vacío = sin SKU, igual que el ``sku_produccion`` del detalle de ventas.
        return (variante.sku or None) if variante else None

    def get_sku_siguiente(self, obj):
        """El SKU que generará ``variante-onboarding`` para esta talla (misma fórmula);
        ``None`` si ya tiene (aunque su variante esté inactiva, #788) o la línea aún no
        tiene color con código (lo arma el front con ``sku_prefijo``)."""
        color = obj.pedido_detalle.color
        ya_tiene = getattr(obj, "tiene_variante", False) or getattr(obj, "variante_produccion", None)
        if ya_tiene or color is None or not color.codigo:
            return None
        return f"MP{obj.pedido_detalle_id}-{color.codigo}-{obj.talla.nombre}".strip().upper()

    class Meta:
        model = PedidoDetalleTalla
        fields = [
            "id",
            "talla_nombre",
            "cantidad",
            "sku_produccion",
            "sku_siguiente",
            "aplica_catalogo",
            "sku_catalogo",
            "lleva_bordado",
            "bordado_config",
            "lleva_reflejante",
            "reflejante_config",
            "lleva_corte_manga",
            "corte_manga_config",
            "lleva_cambio_talla",
            "cambio_talla_config",
            "avance",
        ]


class PedidoDetalleEspecialSerializer(serializers.ModelSerializer):
    color_nombre = serializers.SerializerMethodField()
    # Llenado en la vista via Prefetch(..., to_attr="tallas_especiales"): todas
    # las tallas de la linea especial.
    tallas = PedidoDetalleTallaEspecialSerializer(source="tallas_especiales", many=True, read_only=True)
    categoria_producto = serializers.SerializerMethodField()
    # SKU = ``{sku_prefijo}-{color.codigo}-{talla}``: con él el front muestra el
    # siguiente SKU en cuanto se elige el color.
    sku_prefijo = serializers.SerializerMethodField()

    class Meta:
        model = PedidoDetalle
        fields = ["id", "producto_nombre_externo", "color_nombre", "categoria_producto", "sku_prefijo", "tallas"]

    def get_categoria_producto(self, obj):
        # Llenada en la vista desde la línea de la cotización.
        categoria = getattr(obj, "categoria", None)
        return {"id": categoria.pk, "nombre": categoria.nombre} if categoria else None

    def get_sku_prefijo(self, obj):
        return f"MP{obj.pk}"

    def get_color_nombre(self, obj):
        return obj.color.nombre if obj.color else None


class PedidoEspecialDetailSerializer(serializers.ModelSerializer):
    # Llenado en la vista: solo las lineas con al menos una talla especial.
    detalles = PedidoDetalleEspecialSerializer(source="detalles_especiales", many=True, read_only=True)
    pre_orden = serializers.SerializerMethodField()
    ordenes_servicio = serializers.SerializerMethodField()

    class Meta:
        model = Pedido
        fields = ["id", "folio", "estatus", "activo", "cliente_nombre", "clasificacion", "fecha_confirmacion", "detalles", "pre_orden", "ordenes_servicio"]

    def get_pre_orden(self, obj):
        return resumen_pre_orden_pedido(obj)

    def get_ordenes_servicio(self, obj):
        return ordenes_servicio_pedido(obj)


class VarianteProduccionOnboardingSerializer(serializers.Serializer):
    """Input de ``PedidoEspecialViewSet.variante_onboarding``: una línea
    especial + el detalle de materiales que se replica en el BOM de cada
    talla que se de alta."""
    pedido_detalle_id = serializers.IntegerField()
    # Opcional en el contrato; la vista lo exige si la línea no tiene color.
    # ``Color`` es catálogo global (sin empresa). Aquí sólo se resuelve el id:
    # que esté activo lo decide la vista, porque reenviar el color que la línea
    # YA tiene debe aceptarse aunque ese color se haya desactivado después.
    color = serializers.PrimaryKeyRelatedField(
        queryset=Color.objects.all(), required=False, allow_null=True
    )
    materia_prima_detalle = BomDetalleSerializer(many=True)
    # También da de alta el producto y sus variantes en el catálogo (con la categoría
    # de la línea), para que tenga existencias y se venda como producto normal.
    alta_catalogo = serializers.BooleanField(required=False, default=False)

    def validate_materia_prima_detalle(self, value):
        if not value:
            raise serializers.ValidationError("Se requiere al menos un insumo.")
        return validar_sin_duplicados(value)


class AltaCatalogoInputSerializer(serializers.Serializer):
    """Body de ``PedidoEspecialViewSet.alta_catalogo`` (solo para el esquema OpenAPI)."""
    pedido_detalle_id = serializers.IntegerField()
    # Solo si la línea no tiene categoría; queda guardada en la línea.
    categoria_producto = serializers.IntegerField(required=False)


class VarianteProduccionSerializer(serializers.ModelSerializer):
    talla_nombre = serializers.CharField(source="talla.nombre", read_only=True)
    # SKU de la ``ProductoVariante`` de catálogo creada con ``alta_catalogo``; ``None`` si no.
    sku_catalogo = serializers.SerializerMethodField()

    def get_sku_catalogo(self, obj):
        return getattr(obj, "sku_catalogo", None)

    class Meta:
        model = VarianteProductoProduccion
        fields = ["id", "sku", "nombre", "talla_nombre", "aplica_catalogo", "sku_catalogo", "pedido_detalle"]


class _EditarOrdenServicioSerializer(serializers.ModelSerializer):
    """PATCH del detalle de reflejante/corte de manga: estatus y datos de
    seguimiento. Lo demás (pedido, renglones, folio) no se edita aquí."""

    estatus_field = None
    # Ambos enums comparten valores: 5 = Completado, 7 = Cancelado.

    def validate_prioridad(self, value):
        # 1 Alta, 2 Media, 3 Baja (#776).
        if value not in (1, 2, 3):
            raise serializers.ValidationError("Prioridad inválida: 1 (Alta), 2 (Media) o 3 (Baja).")
        return value

    def validate(self, attrs):
        if getattr(self.instance, self.estatus_field) == 7:
            raise serializers.ValidationError({self.estatus_field: "La orden está cancelada: ya no se edita."})
        return attrs

    def update(self, instance, validated_data):
        nuevo = validated_data.get(self.estatus_field)
        if "fecha_fin" not in validated_data:
            if nuevo == 5 and not instance.fecha_fin:
                # Completado sin fecha de fin capturada: se sella ahora.
                validated_data["fecha_fin"] = timezone.now()
            elif nuevo is not None and nuevo not in (5, 7) and getattr(instance, self.estatus_field) == 5:
                # Reabierta: deja de tener fecha de fin (#777). Cancelar la conserva.
                validated_data["fecha_fin"] = None
        return super().update(instance, validated_data)


class OrdenReflejanteEditarSerializer(_EditarOrdenServicioSerializer):
    estatus_field = "estatus_reflejante"
    estatus_reflejante_display = serializers.CharField(source="get_estatus_reflejante_display", read_only=True)

    class Meta:
        model = OrdenesReflejante
        fields = ["id", "folio_reflejante", "estatus_reflejante", "estatus_reflejante_display",
                  "prioridad", "fecha_fin", "observaciones"]
        read_only_fields = ["id", "folio_reflejante"]


class OrdenesCorteMangaEditarSerializer(_EditarOrdenServicioSerializer):
    estatus_field = "estatus_corte"
    estatus_corte_display = serializers.CharField(source="get_estatus_corte_display", read_only=True)

    class Meta:
        model = OrdenesCorteManga
        fields = ["id", "folio_ocm", "estatus_corte", "estatus_corte_display",
                  "prioridad", "fecha_fin", "observaciones"]
        read_only_fields = ["id", "folio_ocm"]


class PreOrdenProduccionListSerializer(serializers.ModelSerializer):
    """Listado de pre-órdenes. ``piezas_total``/``tallas_total``/``fem`` vienen
    anotados (subconsultas); el resto del seguimiento se deriva sin consultas."""
    estatus_display = serializers.CharField(source="get_estatus_display", read_only=True)
    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True)
    cliente_nombre = serializers.CharField(source="pedido.cliente_nombre", read_only=True, allow_null=True)
    op_folio = serializers.CharField(source="op.folio_op", read_only=True, allow_null=True, default=None)
    piezas_total = serializers.FloatField(read_only=True, default=0.0)
    tallas_total = serializers.IntegerField(read_only=True, default=0)
    fecha_compromiso = serializers.SerializerMethodField()
    dias_en_estatus = serializers.SerializerMethodField()
    fecha_estimada_material = serializers.DateField(source="fem", read_only=True, allow_null=True, default=None)
    regreso = serializers.SerializerMethodField()
    semaforo = serializers.SerializerMethodField()

    class Meta:
        model = PreOrdenProduccion
        fields = [
            "id", "pedido", "pedido_folio", "cliente_nombre", "sucursal", "estatus", "estatus_display",
            "op", "op_folio", "piezas_total", "tallas_total", "fecha_confirmacion_existencias",
            "fecha_compromiso", "dias_en_estatus", "materiales_faltantes", "fecha_estimada_material",
            "regreso", "semaforo", "created_at", "updated_at",
        ]

    def get_fecha_compromiso(self, obj):
        from produccion.services.pre_orden_service import fecha_compromiso
        return fecha_compromiso(obj.pedido)

    def get_dias_en_estatus(self, obj):
        from django.utils import timezone
        desde = obj.estatus_desde or obj.created_at
        return (timezone.localdate() - timezone.localdate(desde)).days if desde else 0

    def get_regreso(self, obj):
        return {"fecha": obj.regreso_fecha, "motivo": obj.regreso_motivo} if obj.regreso_fecha else None

    def get_semaforo(self, obj):
        from produccion.services.pre_orden_service import fecha_compromiso, semaforo
        return semaforo(obj.estatus, obj.materiales_faltantes, getattr(obj, "fem", None), fecha_compromiso(obj.pedido))


class PreOrdenProduccionDetalleSerializer(serializers.ModelSerializer):
    sku = serializers.CharField(source="variante_produccion.sku", read_only=True)
    nombre = serializers.CharField(source="variante_produccion.nombre", read_only=True)
    talla_nombre = serializers.CharField(source="variante_produccion.talla.nombre", read_only=True, allow_null=True, default=None)
    color_nombre = serializers.CharField(source="variante_produccion.color.nombre", read_only=True, allow_null=True, default=None)
    cantidad = serializers.FloatField()

    class Meta:
        model = PreOrdenProduccionDetalle
        fields = ["id", "variante_produccion", "sku", "nombre", "talla_nombre", "color_nombre", "pedido_detalle", "cantidad"]


class PreOrdenProduccionDetailSerializer(PreOrdenProduccionListSerializer):
    """Detalle: tallas, materiales (``context["materiales"]``), necesidades de
    compra y las OC donde están."""
    detalles = PreOrdenProduccionDetalleSerializer(many=True, read_only=True)
    materiales = serializers.SerializerMethodField()
    necesidades = serializers.SerializerMethodField()
    ordenes_compra = serializers.SerializerMethodField()
    usuario_confirma_nombre = serializers.SerializerMethodField()

    class Meta(PreOrdenProduccionListSerializer.Meta):
        fields = PreOrdenProduccionListSerializer.Meta.fields + [
            "usuario_confirma", "usuario_confirma_nombre", "detalles", "materiales", "necesidades", "ordenes_compra",
        ]

    def get_materiales(self, obj):
        dec = ("requerido", "existencia", "en_transito", "comprometido", "faltante", "faltante_real")
        return [
            {**{k: v for k, v in m.items() if k != "unidad_id"}, **{k: float(m[k]) for k in dec}}
            for m in self.context.get("materiales") or []
        ]

    def get_necesidades(self, obj):
        from compras.api.necesidades import NecesidadCompraSerializer
        return NecesidadCompraSerializer(obj.necesidades.all(), many=True).data

    def get_ordenes_compra(self, obj):
        ocs = {n.orden_compra_id: n.orden_compra for n in obj.necesidades.all() if n.orden_compra_id}
        ocs.update({oc.pk: oc for oc in obj.ordenes_compra.all()})
        return [
            {"id": oc.pk, "folio": oc.folio, "estatus": oc.estatus, "estatus_label": oc.get_estatus_display(),
             "proveedor": oc.proveedor_id, "fecha_entrega_estimada": oc.fecha_entrega_estimada}
            for oc in sorted(ocs.values(), key=lambda o: o.pk)
        ]

    def get_usuario_confirma_nombre(self, obj):
        u = obj.usuario_confirma
        return (u.get_full_name().strip() or u.email) if u else None


class PreOrdenEventoSerializer(serializers.ModelSerializer):
    evento_display = serializers.CharField(source="get_evento_display", read_only=True)
    usuario = serializers.SerializerMethodField()

    class Meta:
        model = PreOrdenEvento
        fields = ["id", "fecha", "evento", "evento_display", "usuario", "descripcion"]

    def get_usuario(self, obj):
        u = obj.usuario
        return {"id": u.pk, "nombre": u.get_full_name().strip() or u.email} if u else None
