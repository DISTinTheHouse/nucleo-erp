from django.db import transaction
from rest_framework import serializers

from produccion.models import (
    ListaMaterialBom,
    BomDetalle,
    OrdenProduccion,
    OrdenProduccionDetalle,
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
    OrdenCorteMangaDetalle
)

from catalogo.api.serializers import ProductoVarianteSerializer
from catalogo.models import ProductoVariante
from produccion.services.common import revisar_empresa


class BomDetalleSerializer(serializers.ModelSerializer):
    componente_nombre = serializers.SerializerMethodField()
    unidad_clave = serializers.SerializerMethodField()

    def get_componente_nombre(self, obj):
        return obj.componente.nombre if obj.componente else None

    def get_unidad_clave(self, obj):
        return obj.unidad.clave if obj.unidad else None

    class Meta:
        model = BomDetalle
        fields = '__all__'
        read_only_fields = ['bom', 'activo']

class BomBulkItemSerializer(serializers.Serializer):
    producto_variante_id = serializers.IntegerField()
    bom_id = serializers.IntegerField(allow_null=True)
    detalles = BomDetalleSerializer(many=True)

class ListaMaterialBomSerializer(serializers.ModelSerializer):
    materia_prima_detalle = BomDetalleSerializer(many=True)

    class Meta:
        model = ListaMaterialBom
        fields = '__all__'
        read_only_fields = ['activo', 'bom_id']
    
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
        write_only=True
    )
    bom_detalle = BomDetalleSerializer(
        source='bom.materia_prima_detalle',
        many=True,
        read_only=True
    )

    class Meta:
        model = OrdenProduccionDetalle
        fields = '__all__'
        # 'bom' ya no es parte del contrato del cliente: se resuelve en el
        # servidor a partir del BOM activo de cada producto_variante.
        read_only_fields = ['activo', 'op', 'bom']

class OrdenProduccionSerializer(serializers.ModelSerializer):
    orden_produccion_detalle = OrdenProduccionDetalleSerializer(many=True)

    class Meta:
        model = OrdenProduccion
        fields = '__all__'
        read_only_fields = ['folio_op', 'activo', 'usuario_asignado']

class ConsumoProduccionSerializer(serializers.ModelSerializer):
    detalles = serializers.SerializerMethodField()

    class Meta:
        model = ConsumoProduccion
        fields = ['consumo_produccion_id', 'op', 'detalles']

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

class OrdenBordadoDetalleSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)
    ubicaciones = serializers.SerializerMethodField()
    foto = serializers.SerializerMethodField()
    notas = serializers.SerializerMethodField()
    bordado_config = serializers.SerializerMethodField()

    class Meta:
        model = OrdenBordadoDetalle
        fields = '__all__'

    def _get_pedido_detalle_talla(self, obj):
        if obj.pedido_detalle_id is None or obj.talla_id is None:
            return None
        if not hasattr(self, '_pdt_cache'):
            self._pdt_cache = {}
        key = (obj.pedido_detalle_id, obj.talla_id)
        if key not in self._pdt_cache:
            from ventas.models import PedidoDetalleTalla
            self._pdt_cache[key] = (
                PedidoDetalleTalla.objects
                .filter(pedido_detalle_id=obj.pedido_detalle_id, talla_id=obj.talla_id)
                .only('bordado_config')
                .first()
            )
        return self._pdt_cache[key]

    def _get_cfg(self, obj):
        pdt = self._get_pedido_detalle_talla(obj)
        return getattr(pdt, 'bordado_config', None) or {}

    def get_bordado_config(self, obj):
        cfg = self._get_cfg(obj)
        return cfg or None

    def get_ubicaciones(self, obj):
        cfg = self._get_cfg(obj)
        ubicaciones = cfg.get('ubicaciones')
        return ubicaciones if isinstance(ubicaciones, list) else []

    def get_foto(self, obj):
        cfg = self._get_cfg(obj)
        for key in ('foto', 'imagen', 'imagen_url', 'foto_url'):
            value = cfg.get(key)
            if value:
                if isinstance(value, dict):
                    return value
                return {'url': value}
        return None

    def get_notas(self, obj):
        cfg = self._get_cfg(obj)
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
            'usuario_asignado',
            'estatus_bordado',
            'folio_bordado',
            'empresa',
            'sucursal'
        ]

    def get_usuario_nombre(self, obj):
        usuario = obj.usuario_asignado
        if not usuario: return None
        return usuario.get_full_name().strip() or usuario.email

    def validate(self, attrs):
        """Valida que ``detalles_override[]`` (si viene) sea un arreglo válido
        de renglones del tipo:

            { "pedido_detalle_talla_id": 123, "cantidad": 25 }

        Reglas:
        - No puede haber IDs duplicados.
        - Cada ID debe pertenecer al mismo ``pedido`` que el body (la FK que
          viene en attrs["pedido"]).
        - ``cantidad`` debe ser numérico y > 0.
        - ``cantidad`` no puede ser mayor a ``PedidoDetalleTalla.cantidad``
          original del pedido (SSoT); se permite parcial (<= 100%), pero nunca
          exceder el total contratado.
        """
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
                    try:
                        pdt = PedidoDetalleTalla.objects.select_related("pedido_detalle").get(pk=pdt_id)
                    except PedidoDetalleTalla.DoesNotExist:
                        raise serializers.ValidationError({
                            "detalles_override": f"`pedido_detalle_talla_id={pdt_id}` no existe."
                        })
                    if pdt.pedido_detalle.pedido_id != pedido.pk:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no pertenece "
                                f"al pedido `{pedido.pk}`."
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
        fields = '__all__'


class OrdenBordadoListSerializer(OrdenBordadoSerializer):
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
        return (self.context.get("cobertura") or {}).get(obj.pk) or {}

    def get_cantidad_cubierta(self, obj):
        """Piezas que programa ESTA orden."""
        return self._cobertura(obj).get("cubierto", 0)

    def get_cantidad_contratada(self, obj):
        """Piezas de bordado que contrató el pedido (todas sus líneas)."""
        return self._cobertura(obj).get("contratado", 0)

    def get_cobertura_completa(self, obj):
        """¿Esta orden sola cubre el 100% de lo contratado por el pedido?"""
        return self._cobertura(obj).get("completa", False)


class OrdenBordadoDetalleRetrieveSerializer(OrdenBordadoDetalleSerializer):
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

    def _linea(self, obj):
        mapa = self.context.get("partialidad_por_linea") or {}
        return mapa.get((obj.pedido_detalle_id, obj.talla_id))

    def get_cantidad_pedido(self, obj):
        """Piezas contratadas por el pedido en esta línea."""
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


class OrdenBordadoRetrieveSerializer(OrdenBordadoSerializer):
    """Orden de bordado para el detalle, con contexto de parcialidad.

    Sólo la usa ``retrieve``: ``create`` conserva ``OrdenBordadoSerializer``
    para no alterar lo que devuelve el alta.
    """

    detalles = OrdenBordadoDetalleRetrieveSerializer(many=True, read_only=True)

    otras_ordenes_del_pedido = serializers.SerializerMethodField()
    reparto_por_talla_aproximado = serializers.SerializerMethodField()

    def get_otras_ordenes_del_pedido(self, obj):
        """Las demás OBs activas del mismo pedido, sin incluir ésta."""
        return self.context.get("hermanas") or []

    def get_reparto_por_talla_aproximado(self, obj):
        """¿El reparto por talla es aproximado?

        ``True`` cuando el pedido tiene piezas programadas sin talla
        identificable (renglones con ``talla`` NULL, que genera el pipeline de
        picking). El total por ``pedido_detalle`` sigue siendo exacto; lo que
        no se puede afirmar es a qué talla concreta pertenecen. Hoy no hay
        ningún renglón así en la base.
        """
        return bool(self.context.get("reparto_aproximado", False))


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

    class Meta:
        model = BordadoAvances
        fields = '__all__'
        # ``usuario`` queda escribible (client-supplied, flujo de supervisor
        # en nombre de otro operador) pero validado contra la empresa de la
        # orden padre en ``_OrdenPadreWriteOnceMixin.validate()``.
        read_only_fields = ['activo']

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
        if obj.pedido_detalle_id is None or obj.talla_id is None:
            return None
        if not hasattr(self, '_pdt_reflejante_cache'):
            self._pdt_reflejante_cache = {}
        key = (obj.pedido_detalle_id, obj.talla_id)
        if key not in self._pdt_reflejante_cache:
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

        Aquí no se normaliza nada: es lo que viaja en el campo
        ``reflejante_config`` de la respuesta, y en datos reales es SIEMPRE un
        arreglo (ver ``_get_cfg_dict``).
        """
        pdt = self._get_pedido_detalle_talla(obj)
        return getattr(pdt, 'reflejante_config', None) or {}

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
        """
        cfg = self._get_cfg(obj)
        return cfg if isinstance(cfg, dict) else {}

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
                if pedido is not None:
                    try:
                        pdt = PedidoDetalleTalla.objects.select_related("pedido_detalle").get(pk=pdt_id)
                    except PedidoDetalleTalla.DoesNotExist:
                        raise serializers.ValidationError({
                            "detalles_override": f"`pedido_detalle_talla_id={pdt_id}` no existe."
                        })
                    if pdt.pedido_detalle.pedido_id != pedido.pk:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no pertenece "
                                f"al pedido `{pedido.pk}`."
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
        fields = '__all__'


class OrdenReflejanteListSerializer(OrdenReflejanteSerializer):
    """Orden de reflejante para el listado."""

    # Sólo cambia el anidado de ``detalles``; ver
    # ``OrdenBordadoListSerializer``.

    detalles = OrdenReflejanteDetalleListSerializer(many=True, read_only=True)


class ReflejanteAvancesSerializer(_OrdenPadreWriteOnceMixin, serializers.ModelSerializer):
    orden_padre_field = 'orden_r'

    class Meta:
        model = ReflejanteAvances
        fields = '__all__'
        # ``usuario`` client-supplied a propósito (ver BordadoAvancesSerializer).
        read_only_fields = ['activo']

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
        if obj.pedido_detalle_id is None or obj.talla_id is None:
            return None
        if not hasattr(self, '_pdt_ocm_cache'):
            self._pdt_ocm_cache = {}
        key = (obj.pedido_detalle_id, obj.talla_id)
        if key not in self._pdt_ocm_cache:
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
        cfg_pedido = self._get_cfg(obj)
        if obj.configuracion:
            merged = dict(cfg_pedido) if cfg_pedido else {}
            if isinstance(obj.configuracion, dict):
                merged.update(obj.configuracion)
            return merged or None
        return cfg_pedido or None

    def get_ubicaciones(self, obj):
        cfg = self.get_corte_manga_config(obj) or {}
        ubicaciones = cfg.get('ubicaciones')
        return ubicaciones if isinstance(ubicaciones, list) else []

    def get_foto(self, obj):
        cfg = self.get_corte_manga_config(obj) or {}
        for key in ('foto', 'imagen', 'imagen_url', 'foto_url'):
            value = cfg.get(key)
            if value:
                if isinstance(value, dict):
                    return value
                return {'url': value}
        return None

    def get_notas(self, obj):
        cfg = self.get_corte_manga_config(obj) or {}
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
                if pedido is not None:
                    try:
                        pdt = PedidoDetalleTalla.objects.select_related("pedido_detalle").get(pk=pdt_id)
                    except PedidoDetalleTalla.DoesNotExist:
                        raise serializers.ValidationError({
                            "detalles_override": f"`pedido_detalle_talla_id={pdt_id}` no existe."
                        })
                    if pdt.pedido_detalle.pedido_id != pedido.pk:
                        raise serializers.ValidationError({
                            "detalles_override": (
                                f"`pedido_detalle_talla_id={pdt_id}` no pertenece "
                                f"al pedido `{pedido.pk}`."
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


class OrdenesCorteMangaListSerializer(OrdenesCorteMangaSerializer):
    """Orden de corte de manga para el listado."""

    # Sólo cambia el anidado de ``detalles``; ver
    # ``OrdenBordadoListSerializer``.

    detalles = OrdenCorteMangaDetalleListSerializer(many=True, read_only=True)
