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
from produccion.services.common import config_como_dict, revisar_empresa


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

class OrdenProduccionListSerializer(serializers.ModelSerializer):
    """Serializer minimalista para el LISTADO de OP.

    Solo campos escalares que consume la tabla; sin ``orden_produccion_detalle``
    (el nested pesado) para evitar el N+1. Campos explícitos, no ``__all__``.
    """
    estatus_op_display = serializers.CharField(source="get_estatus_op_display", read_only=True)

    class Meta:
        model = OrdenProduccion
        fields = [
            "op_id", "folio_op", "estatus_op", "estatus_op_display",
            "prioridad", "fecha_inicio", "fecha_fin", "activo",
            "pedido", "sucursal",
        ]

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
        # ``exclude`` en vez de ``fields = '__all__'`` sólo por
        # ``configuracion``: es una columna del modelo, así que ``__all__`` la
        # metería sola en el listado. No cuesta queries (viaja en el mismo
        # SELECT), pero sí peso: es el ``bordado_config`` entero, con todas sus
        # ubicaciones y sus URLs de imagen, por renglón. El listado se mantiene
        # ligero y el detalle —que sí la declara— es quien la publica.
        exclude = ('configuracion',)


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
            programa = 0.0
            porc = 0.0
            porc_punt = 0.0
            por_detalle.append({
                "orden_bordado_detalle_id": None,
                "producto_id": None,
                "producto_nombre": "Sin talla/SKU asignado (registro antiguo)",
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
        read_only_fields = ['activo', 'usuario']

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

    def validate(self, attrs):
        attrs = super().validate(attrs)

        ob_id = None
        if self.instance is None:
            ob = attrs.get(self.orden_padre_field)
            if ob is not None:
                ob_id = ob.pk
        else:
            ob_instancia = getattr(self.instance, self.orden_padre_field, None)
            if ob_instancia is not None:
                ob_id = ob_instancia.pk

        detalle = attrs.get("orden_bordado_detalle")
        if detalle is not None and ob_id is not None:
            if int(detalle.ob_id) != int(ob_id):
                raise serializers.ValidationError({
                    "orden_bordado_detalle": (
                        "Este detalle-talla no pertenece a la orden de bordado seleccionada."
                    )
                })

        pdt = attrs.get("pedido_detalle_talla")
        if pdt is not None and detalle is not None:
            if (
                getattr(pdt, "pedido_detalle_id", None) is not None
                and getattr(detalle, "pedido_detalle_id", None) is not None
                and int(pdt.pedido_detalle_id) != int(detalle.pedido_detalle_id)
            ):
                raise serializers.ValidationError({
                    "pedido_detalle_talla": (
                        "El PedidoDetalleTalla no coincide con el pedido_detalle del renglón de la OB."
                    )
                })
            if (
                getattr(pdt, "talla_id", None) is not None
                and getattr(detalle, "talla_id", None) is not None
                and int(pdt.talla_id) != int(detalle.talla_id)
            ):
                raise serializers.ValidationError({
                    "pedido_detalle_talla": (
                        "La talla del PedidoDetalleTalla no coincide con la talla del renglón de la OB."
                    )
                })

        return attrs

    def update(self, instance, validated_data):
        validated_data.pop("orden_bordado_detalle", None)
        validated_data.pop("pedido_detalle_talla", None)
        return super().update(instance, validated_data)

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
        # Mismo motivo que en ``OrdenBordadoDetalleListSerializer``: sin este
        # ``exclude``, ``__all__`` publicaría el ``reflejante_config`` entero
        # en cada renglón del listado.
        exclude = ('configuracion',)


class OrdenReflejanteListSerializer(OrdenReflejanteSerializer):
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


class OrdenReflejanteDetalleRetrieveSerializer(OrdenReflejanteDetalleSerializer):
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


class OrdenesCorteMangaRetrieveSerializer(OrdenesCorteMangaSerializer):
    """Orden de corte de manga para el detalle.

    Hereda del serializer base —no del de listado— para conservar el renglón
    completo (``OrdenCorteMangaDetalleSerializer`` con
    ``corte_manga_config``/``ubicaciones``/``foto``/``notas``). Añade
    ``pedido_vinculado`` sólo en el detalle, igual que
    ``OrdenReflejanteRetrieveSerializer``/``OrdenBordadoRetrieveSerializer``.
    """

    pedido_vinculado = serializers.SerializerMethodField()

    def get_pedido_vinculado(self, obj):
        from produccion.services.orden_bordado_field_filter_service import armar_pedido_vinculado
        return armar_pedido_vinculado(obj)
