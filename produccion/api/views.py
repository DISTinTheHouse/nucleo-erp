from django.db.models import Prefetch, prefetch_related_objects
from django.db import transaction
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from ventas.models import Pedido, PedidoDetalleTalla
from usuarios.models import Usuario

from produccion.services.common import config_como_dict, pendientes_por_linea

from produccion.models import (
    ListaMaterialBom,
    BomDetalle,
    OrdenProduccion,
    OrdenProduccionDetalle,
    ConsumoProduccion,
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

from produccion.api.serializers import (
    ListaMaterialBomSerializer,
    BomDetalleSerializer,
    BomBulkItemSerializer,
    OrdenProduccionSerializer,
    OrdenProduccionListSerializer,
    ConsumoProduccionSerializer,
    ProductoTerminadoEntradasSerializer,
    OrdenBordadoSerializer,
    OrdenBordadoListSerializer,
    OrdenBordadoRetrieveSerializer,
    BordadoAvancesSerializer,
    BordadoIncidenciasSerializer,
    OrdenReflejanteSerializer,
    OrdenReflejanteListSerializer,
    OrdenReflejanteRetrieveSerializer,
    ReflejanteAvancesSerializer,
    ReflejanteIncidenciasSerializer,
    OrdenesCorteMangaSerializer,
    OrdenesCorteMangaListSerializer,
    OrdenesCorteMangaRetrieveSerializer
)

from produccion.services.orden_bordado_service import OrdenBordadoService
from produccion.services.orden_reflejante_service import OrdenReflejanteService
from produccion.services.orden_produccion_service import OrdenProduccionService
from produccion.services.orden_corte_manga_service import OrdenCorteMangaService


def _tallas_ot_prefetch(flag):
    """``Prefetch`` de las tallas elegibles para una orden de trabajo.

    Filtra en la BD por el flag y por ``cantidad > 0`` —el mismo criterio que
    ``services.common.tallas_orden_trabajo_qs`` aplica al crear la orden—, así
    que el GET deja de ofrecer renglones que el POST rechaza.

    Sustituye a filtrar con ``det.tallas.filter(...)`` dentro del loop: eso
    ignora la caché del prefetch y dispara una query por ``PedidoDetalle``,
    dejando además inservible el ``prefetch_related`` del queryset.
    """
    return Prefetch(
        "detalles__tallas",
        queryset=(
            PedidoDetalleTalla.objects
            .filter(**{flag: True}, cantidad__gt=0)
            .select_related("talla")
            .order_by("id")
        ),
    )


def _payload_pedidos_onboarding(pedidos_qs, config_attr, cantidades_asignadas_fn,
                                incluir_config_crudo=False):
    """Arma la lista ``pedidos`` del GET de onboarding de órdenes de trabajo.

    Compartido por Bordado / Reflejante / Corte de Manga: los tres emitían el
    mismo bloque verbatim y sólo divergen en el flag de la talla (ya resuelto
    por el ``Prefetch``) y en el campo de configuración (``config_attr``).

    Por línea agrega ``cantidad_asignada``/``cantidad_pendiente`` sobre lo ya
    programado en órdenes activas, y descarta el pedido que no tenga ninguna
    línea con saldo. El descarte se decide **antes** de construir los dicts:
    parsear ``*_config``, ubicaciones, foto y notas de un pedido ya cubierto
    era trabajo que se tiraba a la basura.

    ``incluir_config_crudo`` añade a cada línea el ``*_config`` SIN NORMALIZAR,
    bajo una clave con el nombre del propio campo (``config_attr``). Es opt-in
    porque los cuatro campos derivados de arriba —``posicion_sugerida``,
    ``ubicaciones``, ``foto``, ``notas``— sólo saben leer un config con forma de
    OBJETO, que es la de bordado y corte de manga; el de reflejante es un
    ARREGLO, así que ``config_como_dict`` lo convierte en ``{}`` y esos cuatro
    salen vacíos: el endpoint devolvía 200 pero SIN un solo dato de reflejante,
    y el Paso 2 del alta de OR no tenía nada que pintar.

    No se emite para los tres por igual —que sería lo genérico— porque eso
    cambiaría el shape de las respuestas de Bordado y Corte de Manga, que hoy
    ya son correctas. Se activa sólo donde falta el dato.
    """
    pedidos_lista = list(pedidos_qs)
    por_linea, sin_talla = cantidades_asignadas_fn([p.id for p in pedidos_lista])

    pedidos_payload = []
    for p in pedidos_lista:
        pares = [(det, dt) for det in p.detalles.all() for dt in det.tallas.all()]
        pendientes = pendientes_por_linea(
            [(det.id, dt.talla_id, float(dt.cantidad or 0)) for det, dt in pares],
            por_linea,
            sin_talla,
        )
        if not any(pendiente > 0 for _asignada, pendiente in pendientes):
            continue

        lineas = []
        for (det, dt), (cantidad_asignada, cantidad_pendiente) in zip(pares, pendientes):
            # ``config_como_dict``, no ``or {}``: ``reflejante_config`` es un
            # ARREGLO en el 100% de las filas reales, así que las cinco lecturas
            # de ``cfg`` de aquí abajo (``ubicaciones``, ``foto``, ``notas``,
            # ``posicion``) reventaban con ``AttributeError: 'list' object has
            # no attribute 'get'`` y el GET de onboarding de OR respondía 500.
            # El helper es el mismo que ya usa el retrieve (ver
            # ``services.common.config_como_dict``).
            cfg_crudo = getattr(dt, config_attr, None)
            cfg = config_como_dict(cfg_crudo)
            ubicaciones = cfg.get("ubicaciones") or []
            if isinstance(ubicaciones, list) and ubicaciones:
                primera_ubic = ubicaciones[0] or {}
            else:
                primera_ubic = {}
            foto = None
            for k in ("foto", "imagen", "imagen_url", "foto_url"):
                v = cfg.get(k)
                if v:
                    foto = {"url": v} if isinstance(v, str) else v
                    break
            notas = next(
                (cfg[k] for k in ("notas", "observaciones", "comentarios") if cfg.get(k)),
                None,
            )
            linea = {
                "pedido_detalle_talla_id": dt.id,
                "pedido_detalle_id": det.id,
                "producto_id": det.producto_id,
                "producto_nombre": getattr(det.producto, "nombre", None),
                "talla_id": getattr(dt.talla, "id", None),
                "talla_nombre": getattr(dt.talla, "nombre", None),
                "color_id": getattr(det, "color_id", None),
                "color_nombre": getattr(getattr(det, "color", None), "nombre", None),
                "cantidad_pedido": float(dt.cantidad or 0),
                "cantidad_asignada": cantidad_asignada,
                "cantidad_pendiente": cantidad_pendiente,
                "posicion_sugerida": (
                    cfg.get("posicion")
                    or primera_ubic.get("codigo")
                    or primera_ubic.get("nombre")
                    or None
                ),
                "ubicaciones": ubicaciones if isinstance(ubicaciones, list) else [],
                "foto": foto,
                "notas": notas,
            }
            if incluir_config_crudo:
                # El config ÍNTEGRO, con todos sus elementos y sus claves
                # verbatim. Misma clave, misma forma y mismo ``or None`` que
                # publica el retrieve (``OrdenReflejanteDetalleSerializer
                # .get_reflejante_config``), para que el mismo nombre signifique
                # lo mismo en los dos endpoints.
                linea[config_attr] = cfg_crudo or None
            lineas.append(linea)

        pedidos_payload.append({
            "id": p.id,
            "folio": p.folio,
            "cliente": p.cliente_id,
            "cliente_nombre": getattr(p.cliente, "nombre", None),
            "sucursal": p.sucursal_id,
            "sucursal_nombre": getattr(p.sucursal, "nombre", None),
            "detalles": lineas,
        })
    return pedidos_payload


def _con_detalles_prefetcheados(orden, detalle_model):
    """Prefetchea ``detalles`` sobre una orden recién creada, antes de serializarla.

    El ``select_related``/``Prefetch`` de ``get_queryset()`` sólo cubre
    ``list``/``retrieve``: las respuestas de ``create`` y del POST de
    ``onboarding`` serializan el objeto que devuelve el service, que nunca pasa
    por ese queryset. Como el service hace ``bulk_create`` de los renglones,
    ``orden.detalles.all()`` volvía a consultar la BD y cada renglón resolvía
    ``producto`` y ``talla`` por separado —2 queries extra por renglón—.

    Se prefetchea sobre la instancia en vez de re-consultarla por
    ``get_queryset()`` para no depender del filtro de tenant (la orden recién
    creada siempre cae dentro, pero un ``.get()`` que fallara daría un 500 en
    lugar de una respuesta válida). ``empresa``/``sucursal``/``pedido``/
    ``usuario_asignado`` ya vienen cacheados como objetos desde el service, así
    que no necesitan ``select_related`` aquí.
    """
    prefetch_related_objects(
        [orden],
        Prefetch(
            "detalles",
            queryset=detalle_model.objects.select_related("producto", "talla", "color"),
        ),
    )
    return orden


class ListaMaterialBomViewSet(viewsets.ModelViewSet):
    serializer_class = ListaMaterialBomSerializer

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)

        if empresa is None:
            return ListaMaterialBom.objects.none()
        
        queryset = ListaMaterialBom.objects.filter(empresa=empresa).prefetch_related(
            'materia_prima_detalle__componente',
            'materia_prima_detalle__unidad',
        )

        producto_variante_id = self.request.query_params.get('producto_variante_id')

        if producto_variante_id is not None:
            try:
                producto_variante_id = int(producto_variante_id)
            except ValueError:
                raise ValidationError({"producto_variante_id": "Must be an integer."})

            queryset = queryset.filter(producto_variante_id=producto_variante_id)

        return queryset

    @action(detail=False, methods=['get'], url_path='bulk')
    def bulk(self, request):
        raw = request.query_params.get('producto_variante_ids', '').strip()
        if not raw:
            raise ValidationError({'producto_variante_ids': 'This parameter is required.'})

        try:
            ids = [int(v.strip()) for v in raw.split(',') if v.strip()]
        except ValueError:
            raise ValidationError({'producto_variante_ids': 'All values must be integers.'})

        if not ids:
            raise ValidationError({'producto_variante_ids': 'This parameter is required.'})

        empresa = getattr(request.user, 'empresa', None)
        if empresa is None:
            return Response([], status=status.HTTP_200_OK)

        boms = ListaMaterialBom.objects.filter(
            producto_variante_id__in=ids,
            activo=True,
            empresa=empresa,
        )
        bom_by_variante = {bom.producto_variante_id: bom for bom in boms}

        all_detalles = BomDetalle.objects.filter(
            bom_id__in=[bom.bom_id for bom in bom_by_variante.values()],
            activo=True,
        ).select_related('componente', 'unidad')

        detalles_by_bom = {}
        for detalle in all_detalles:
            detalles_by_bom.setdefault(detalle.bom_id, []).append(detalle)

        result = []
        for variante_id in ids:
            bom = bom_by_variante.get(variante_id)
            if bom is None:
                continue
            result.append({
                'producto_variante_id': variante_id,
                'bom_id': bom.bom_id,
                'detalles': detalles_by_bom.get(bom.bom_id, []),
            })

        return Response(BomBulkItemSerializer(result, many=True).data)

class BomDetalleViewSet(viewsets.ModelViewSet):
    serializer_class = BomDetalleSerializer

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)

        if empresa is None:
            return BomDetalle.objects.none()
        
        queryset = BomDetalle.objects.filter(bom__empresa=empresa)

        return queryset

class OrdenProduccionViewSet(viewsets.ModelViewSet):
    queryset = OrdenProduccion.objects.all()
    serializer_class = OrdenProduccionSerializer

    def get_serializer_class(self):
        if getattr(self, "action", None) == "list":
            return OrdenProduccionListSerializer
        return OrdenProduccionSerializer

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None: return OrdenProduccion.objects.none()
        # Listado más reciente primero; el desempate usa la PK real del modelo
        # (``op_id``, no ``id``).
        queryset = (
            OrdenProduccion.objects
            .filter(empresa=empresa)
            .order_by("-fecha_inicio", "-op_id")
        )
        # El nested ``orden_produccion_detalle`` (con su cadena de FKs) solo lo
        # consumen retrieve/create/update; el listado usa un serializer ligero
        # que no lo toca, así que evitamos el prefetch pesado en ``list``.
        if getattr(self, "action", None) != "list":
            # Los cuatro FKs de encabezado alimentan las etiquetas legibles del
            # serializer (``empresa_nombre``/``sucursal_nombre``/``pedido_folio``/
            # ``usuario_nombre``). Sin este ``select_related`` cada una dispararía
            # una query lazy en retrieve; el listado usa el serializer ligero y no
            # las toca, por eso solo se aplica fuera de ``list``.
            queryset = queryset.select_related(
                "empresa", "sucursal", "pedido", "usuario_asignado"
            ).prefetch_related(
                Prefetch(
                    "orden_produccion_detalle",
                    queryset=OrdenProduccionDetalle.objects.select_related(
                        "producto_variante",
                        "producto_variante__producto",
                        "producto_variante__color",
                        "producto_variante__talla",
                        "bom",
                    ).prefetch_related(
                        Prefetch(
                            "bom__materia_prima_detalle",
                            queryset=BomDetalle.objects.select_related("componente", "unidad"),
                        ),
                    ),
                ),
            )
        return queryset
    
    def save_op(self, request):
        return self._crear_op_desde_request(request)
    
    def get_op_detalle(self, request):
        op_id = request.query_params.get('op_id', None)
        if op_id is None:
            return Response({'msg': 'No se proporcionó orden de producción'}, status=status.HTTP_400_BAD_REQUEST)
        # Aislamiento multi-tenant: esta acción es ``detail=False`` y NO pasa por
        # ``get_queryset()``, así que sin esta verificación cualquier usuario
        # autenticado podría leer la OP de otra empresa adivinando el ``op_id``
        # (IDOR). El servicio hace ``filter(pk=op_id)`` sin filtro de empresa. Se
        # valida la pertenencia aquí, en la capa de vista, antes de delegar. Se
        # responde 404 (no 403) para no revelar la existencia del documento a
        # otra empresa —mismo criterio que ``retrieve`` en OB/OR/OCM—.
        empresa = getattr(request.user, 'empresa', None)
        es_propia = (
            empresa is not None
            and OrdenProduccion.objects.filter(pk=op_id, empresa=empresa).exists()
        )
        if not es_propia:
            return Response({'msg': 'Orden de producción no encontrada'}, status=status.HTTP_404_NOT_FOUND)
        res_data = OrdenProduccionService.get_formatted_op_detalle(op_id)
        if res_data is None:
            return Response({'msg': 'Orden de producción no encontrada'}, status=status.HTTP_404_NOT_FOUND)
        return Response(res_data)

    def _crear_op_desde_request(self, request):
        empresa = getattr(request.user, 'empresa', None)
        if empresa is None:
            return Response(
                {'msg': 'El usuario no tiene una empresa asignada'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            for detalle in serializer.validated_data.get('orden_produccion_detalle', []):
                producto_variante = detalle.get('producto_variante')
                producto_variante_id = getattr(producto_variante, 'pk', None)
                try:
                    bom = ListaMaterialBom.objects.get(
                        producto_variante_id=producto_variante_id,
                        activo=True,
                        empresa=empresa,
                    )
                except ListaMaterialBom.DoesNotExist:
                    raise ValidationError({
                        'orden_produccion_detalle': (
                            f"No existe un BOM activo para el producto_variante_id "
                            f"{producto_variante_id} en la empresa actual."
                        )
                    })
                detalle['bom'] = bom

            result = OrdenProduccionService.save_orden_produccion(
                serializer.validated_data,
                request.user,
                request=request,
            )

        op = result["op"]
        return Response(
            {
                'msg': 'Orden de producción creada exitosamente',
                'op_id': op.op_id,
                'folio_op': op.folio_op,
                'consumo_produccion_id': result["consumo_produccion"].consumo_produccion_id,
                'movimiento_inventario_id': result["movimiento_inventario"].pk,
                'movimiento_id': getattr(result["auditoria_evento"], 'id_evento', None),
            },
            status=status.HTTP_201_CREATED,
        )

    def create(self, request, *args, **kwargs):
        return self._crear_op_desde_request(request)
    
    @action(detail=False, methods=['get', 'post'], url_path='onboarding')
    def onboarding(self, request):
        if request.method == 'GET':
            return self.get_op_detalle(request)
        return self._crear_op_desde_request(request)

class ConsumoProduccionViewSet(viewsets.ModelViewSet):
    queryset = ConsumoProduccion.objects.all().select_related('op').prefetch_related('detalles__producto')
    serializer_class = ConsumoProduccionSerializer
    http_method_names = ['get', 'post']

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None:
            return ConsumoProduccion.objects.none()
        return self.queryset.filter(op__empresa=empresa)

    @action(detail=True, methods=['post'])
    def confirmar(self, request, pk=None):
        return Response({'msg': 'ConsumoProduccionViewSet.confirmar'}, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def anular(self, request, pk=None):
        return Response({'msg': 'ConsumoProduccionViewSet.anular'}, status=status.HTTP_200_OK)

class ProductoTerminadoEntradasViewSet(viewsets.ModelViewSet):
    queryset = ProductoTerminadoEntradas.objects.all()
    serializer_class = ProductoTerminadoEntradasSerializer
    http_method_names = ['get', 'post']

    @action(detail=True, methods=['post'])
    def confirmar(self, request, pk=None):
        return Response({'msg': 'ProductoTerminadoEntradasViewSet.confirmar'}, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['post'])
    def anular(self, request, pk=None):
        return Response({'msg': 'ProductoTerminadoEntradasViewSet.anular'}, status=status.HTTP_200_OK)

class OrdenBordadoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin, GenericViewSet):
    queryset = OrdenesBordado.objects.filter()
    serializer_class = OrdenBordadoSerializer

    def get_serializer_class(self):
        action = getattr(self, "action", None)
        if action == "list":
            return OrdenBordadoListSerializer
        if action == "retrieve":
            return OrdenBordadoRetrieveSerializer
        return OrdenBordadoSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()

    def get_queryset(self):
        """Aislamiento multi-tenant: empresa + sucursal.

        Mismo criterio que ``PickingViewSet``/``PackingViewSet``/
        ``DespachoViewSet``/``TransferenciaViewSet``: sin empresa no se ve
        nada, el superusuario ve todo y el admin de empresa ve todas las
        sucursales de la suya; el resto queda acotado a
        ``sucursales_permitidas()``. ``list`` devuelve ``200 []`` fuera de
        alcance y ``retrieve`` de otra empresa/sucursal devuelve ``404``
        (no ``403``): no se revela la existencia del documento.

        ``select_related``/``prefetch_related`` cortan el N+1: el serializer
        resuelve ``pedido`` (``pedido_folio``), ``usuario_asignado``
        (``usuario_nombre``) y ``empresa``/``sucursal``
        (``empresa_nombre``/``sucursal_nombre``) por orden, y por renglón de
        ``detalles``, ``producto``/``talla``/``color``.
        """
        user = self.request.user
        qs = (
            OrdenesBordado.objects.filter(activo=True)
            .select_related("pedido", "usuario_asignado", "empresa", "sucursal")
            .prefetch_related(
                Prefetch(
                    "detalles",
                    # ``order_by`` local al Prefetch, no ``Meta.ordering``: el
                    # modelo no declara orden y el de la BD no es estable, así
                    # que ``detalles`` salía en distinto orden en list y en
                    # retrieve. Ponerlo en el ``Meta`` afectaría toda consulta
                    # al modelo (admin incluido); aquí sólo afecta a esta vista.
                    queryset=OrdenBordadoDetalle.objects.select_related(
                        "producto", "talla", "color"
                    ).order_by("id"),
                )
            )
            # Listado más reciente primero; ``-id`` como desempate estable.
            .order_by("-fecha_inicio", "-id")
        )

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        # list → renglón ligero, sin los campos que obligan a re-leer
        # ``PedidoDetalleTalla`` una vez por renglón (ver
        # ``OrdenBordadoDetalleListSerializer``), más los tres campos de
        # cobertura. retrieve → el serializer completo más el contexto de
        # parcialidad por línea. ``create`` conserva ``OrdenBordadoSerializer``
        # tal cual, para no alterar lo que devuelve el alta.
        # Mismo patrón que ``TransferenciaViewSet``.
        if self.action == "list":
            return OrdenBordadoListSerializer
        if self.action == "retrieve":
            return OrdenBordadoRetrieveSerializer
        return OrdenBordadoSerializer

    def _serializar_pagina(self, ordenes):
        """Serializa un conjunto de órdenes con la cobertura ya resuelta.

        La cobertura se calcula para el conjunto ENTERO antes de serializar: 2
        queries agrupadas, constantes, en vez de dos por fila.
        """
        return self.get_serializer(
            ordenes,
            many=True,
            context={
                **self.get_serializer_context(),
                "cobertura": OrdenBordadoService.cobertura_por_orden(ordenes),
            },
        )

    def list(self, request, *args, **kwargs):
        """Listado con la cobertura de cada orden sobre su pedido.

        Réplica de ``ListModelMixin.list`` —incluida la paginación, que la
        versión anterior de este override omitía: sin ``paginate_queryset`` este
        endpoint habría ignorado en silencio cualquier
        ``DEFAULT_PAGINATION_CLASS`` que se configurara después, devolviendo el
        arreglo completo mientras el resto de los listados sí paginaba—. Lo
        único que añade es el contexto de cobertura.
        """
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(self._serializar_pagina(page).data)

        return Response(self._serializar_pagina(list(queryset)).data)

    def retrieve(self, request, *args, **kwargs):
        """Detalle con el contexto de parcialidad del pedido.

        Resuelve una sola vez —para todos los renglones— lo contratado, lo ya
        asignado por las OBs activas y las órdenes hermanas. La cobertura
        (``cantidad_cubierta``/``cantidad_contratada``/``cobertura_completa``)
        también viaja aquí: el serializer del detalle hereda del de listado, así
        que el diálogo puede enunciar "cubre 7 de 40" sin leer además la fila
        del listado.
        """
        orden = self.get_object()
        por_linea, por_detalle, hermanas, aproximado = (
            OrdenBordadoService.partialidad_de_orden(orden)
        )
        avances = list(
            BordadoAvances.objects
            .filter(ob=orden, activo=True)
            .select_related("usuario")
            .order_by("-fecha", "-id")
        )
        serializer = self.get_serializer(
            orden,
            context={
                **self.get_serializer_context(),
                "cobertura": OrdenBordadoService.cobertura_por_orden([orden]),
                "partialidad_por_linea": por_linea,
                "partialidad_por_detalle": por_detalle,
                "hermanas": hermanas,
                "reparto_aproximado": aproximado,
                "avances": avances,
            },
        )
        return Response(serializer.data)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orden_bordado = OrdenBordadoService.save(serializer.validated_data, request.user)
        return Response(
            OrdenBordadoSerializer(
                _con_detalles_prefetcheados(orden_bordado, OrdenBordadoDetalle)
            ).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        """Onboarding para OrdenBordado (patrón WMS picking/packing/despacho).

        GET → catálogos de pedidos con prendas que requieren bordado,
        operadores de la empresa, preview del folio siguiente y un detalle
        por pedido con las líneas elegibles (producto/talla/color/cantidad
        + ubicaciones/foto del bordado_config). Este detalle permite al
        frontend armar un selector de cantidades antes de crear la OB.

        POST → mismo save que create() (comparte serializer y service).
        Body opcional `detalles_override[]` permite que el usuario elija
        por cada línea qué cantidad incluir en la OB (en vez del 100%).
        """
        if request.method == "GET":
            user = request.user
            empresa = getattr(user, "empresa", None)
            empty = {"pedidos": [], "operadores": [], "preview": {"folio_ob_sugerido": None}}
            if empresa is None:
                return Response(empty)

            sucursal_ids = user.sucursales_permitidas()
            if not sucursal_ids:
                return Response(empty)

            pedidos_qs = (
                Pedido.objects.filter(
                    empresa=empresa,
                    sucursal_id__in=sucursal_ids,
                    activo=True,
                    detalles__tallas__lleva_bordado=True,
                )
                .distinct()
                .select_related("cliente", "sucursal")
                .prefetch_related(
                    "detalles",
                    _tallas_ot_prefetch("lleva_bordado"),
                    "detalles__producto",
                    "detalles__color",
                )
                .order_by("-created_at", "-id")
            )

            operadores_qs = (
                Usuario.objects.filter(empresa=empresa, is_active=True)
                .order_by("first_name", "last_name", "email")
            )

            preview_folio = None
            sucursal_default = getattr(user, "sucursal_default", None)
            if sucursal_default is not None:
                try:
                    from produccion.utils.folios import preview_ob_folio

                    preview_folio = preview_ob_folio(empresa.pk, sucursal_default.pk)
                except Exception:
                    preview_folio = None

            pedidos_payload = _payload_pedidos_onboarding(
                pedidos_qs,
                "bordado_config",
                OrdenBordadoService.cantidades_asignadas_por_pedidos,
            )

            return Response({
                "pedidos": pedidos_payload,
                "operadores": [
                    {"id": u.id, "nombre": u.get_full_name().strip() or u.email}
                    for u in operadores_qs
                ],
                "preview": {"folio_ob_sugerido": preview_folio},
            })

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orden_bordado = OrdenBordadoService.save(serializer.validated_data, request.user)
        return Response(
            OrdenBordadoSerializer(
                _con_detalles_prefetcheados(orden_bordado, OrdenBordadoDetalle)
            ).data,
            status=status.HTTP_201_CREATED,
        )

class _OrdenPadreTenantScopedMixin:
    """``get_queryset`` multi-tenant a través de la FK a la orden padre.

    Los modelos satélite (Avances/Incidencias de Bordado y Reflejante) no
    tienen ``empresa``/``sucursal`` propios: el tenant sólo se alcanza
    atravesando ``orden_padre_field`` (``ob``/``orden_r``). Mismo criterio que
    ``OrdenBordadoViewSet.get_queryset()``, pero sobre la orden padre. Una sola
    definición para los cuatro ViewSets; cada uno declara su ``orden_padre_field``.
    """

    #: Nombre de la FK a la orden padre en el modelo satélite.
    orden_padre_field = None

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset.model.objects.filter(activo=True)

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(**{f"{self.orden_padre_field}__empresa": empresa})
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(
            **{f"{self.orden_padre_field}__sucursal_id__in": user.sucursales_permitidas()}
        )


class BordadoAvancesViewSet(_OrdenPadreTenantScopedMixin, viewsets.ModelViewSet):
    queryset = BordadoAvances.objects.filter(activo=True)
    serializer_class = BordadoAvancesSerializer
    orden_padre_field = "ob"

    def perform_create(self, serializer):
        serializer.save(usuario=self.request.user)

    def perform_destroy(self, instance):
        instance.soft_delete()

class BordadoIncidenciasViewSet(_OrdenPadreTenantScopedMixin, viewsets.ModelViewSet):
    queryset = BordadoIncidencias.objects.filter(activo=True)
    serializer_class = BordadoIncidenciasSerializer
    orden_padre_field = "ob"

    def perform_destroy(self, instance):
        instance.soft_delete()

class OrdenReflejanteViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    mixins.CreateModelMixin, GenericViewSet
    ):
    queryset = OrdenesReflejante.objects.all()
    serializer_class = OrdenReflejanteSerializer

    def get_queryset(self):
        """Aislamiento multi-tenant: empresa + sucursal.

        Mismo criterio que ``OrdenBordadoViewSet``/``PickingViewSet``.

        El ``select_related``/``prefetch_related`` corta el N+1 del serializer:
        cada orden resolvía ``empresa``/``sucursal`` (``*_nombre``), ``pedido``
        (``pedido_folio``) y ``usuario_asignado`` (``usuario_nombre``) en
        consultas sueltas, y cada renglón de ``detalles`` resolvía
        ``producto``/``talla``/``color`` en otras más. Contra el pooler de
        Supabase cada ida y vuelta cuesta ~85 ms, así que el costo del endpoint
        lo dominaba el **número** de queries, no su peso: 8 queries para 1 sola
        orden. Con esto el list queda en 2 queries constantes, sin importar
        cuántas órdenes o renglones traiga.
        """
        user = self.request.user
        qs = (
            OrdenesReflejante.objects.filter(activo=True)
            .select_related("empresa", "sucursal", "pedido", "usuario_asignado")
            .prefetch_related(
                Prefetch(
                    "detalles",
                    # Orden estable local al Prefetch; ver el equivalente en
                    # ``OrdenBordadoViewSet``.
                    queryset=OrdenReflejanteDetalle.objects.select_related(
                        "producto", "talla", "color"
                    ).order_by("id"),
                )
            )
            # Listado más reciente primero; ``-id`` como desempate estable.
            .order_by("-fecha_inicio", "-id")
        )

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        # Ver ``OrdenBordadoViewSet.get_serializer_class``.
        if self.action == "list":
            return OrdenReflejanteListSerializer
        if self.action == "retrieve":
            return OrdenReflejanteRetrieveSerializer
        return OrdenReflejanteSerializer

    def _serializar_pagina(self, ordenes):
        """Serializa un conjunto de órdenes con la cobertura ya resuelta.

        La cobertura se calcula para el conjunto ENTERO antes de serializar: 2
        queries agrupadas, constantes, en vez de dos por fila.
        """
        return self.get_serializer(
            ordenes,
            many=True,
            context={
                **self.get_serializer_context(),
                "cobertura": OrdenReflejanteService.cobertura_por_orden(ordenes),
            },
        )

    def list(self, request, *args, **kwargs):
        """Listado con la cobertura de cada orden sobre su pedido.

        Réplica de ``ListModelMixin.list`` —incluida la paginación, que sin
        ``paginate_queryset`` este endpoint ignoraría en silencio si algún día
        se configurara un ``DEFAULT_PAGINATION_CLASS``, devolviendo el arreglo
        completo mientras el resto de los listados sí pagina—. Lo único que
        añade es el contexto de cobertura. Mismo patrón que
        ``OrdenBordadoViewSet.list``.
        """
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(self._serializar_pagina(page).data)

        return Response(self._serializar_pagina(list(queryset)).data)

    def retrieve(self, request, *args, **kwargs):
        """Detalle con el contexto de parcialidad del pedido.

        Resuelve una sola vez —para todos los renglones— lo contratado, lo ya
        asignado por las ORs activas y las órdenes hermanas. La cobertura
        (``cantidad_cubierta``/``cantidad_contratada``/``cobertura_completa``)
        también viaja aquí: el serializer del detalle hereda del de listado, así
        que el diálogo puede enunciar "cubre 7 de 40" sin leer además la fila
        del listado.
        """
        orden = self.get_object()
        por_linea, por_detalle, hermanas, aproximado = (
            OrdenReflejanteService.partialidad_de_orden(orden)
        )
        serializer = self.get_serializer(
            orden,
            context={
                **self.get_serializer_context(),
                "cobertura": OrdenReflejanteService.cobertura_por_orden([orden]),
                "partialidad_por_linea": por_linea,
                "partialidad_por_detalle": por_detalle,
                "hermanas": hermanas,
                "reparto_aproximado": aproximado,
            },
        )
        return Response(serializer.data)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orden_reflejante = OrdenReflejanteService.save(serializer.validated_data, request.user)
        return Response(
            OrdenReflejanteSerializer(
                _con_detalles_prefetcheados(orden_reflejante, OrdenReflejanteDetalle)
            ).data,
            status=status.HTTP_200_OK,
        )

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        """Onboarding para OrdenReflejante (patrón WMS picking/packing/despacho).

        GET → catálogos de pedidos con prendas que requieren reflejante,
        operadores de la empresa, preview del folio siguiente y detalle
        por pedido con líneas elegibles (producto/talla/color y cantidad
        del pedido + ubicaciones/foto de reflejante_config). El selector
        de cantidades en Next.js usa este detalle para armar el POST.

        POST → mismo save que create() (comparte serializer y service).
        Body opcional `detalles_override[]` permite seleccionar líneas
        y cantidades para OR parciales.
        """
        if request.method == "GET":
            user = request.user
            empresa = getattr(user, "empresa", None)
            empty = {"pedidos": [], "operadores": [], "preview": {"folio_or_sugerido": None}}
            if empresa is None:
                return Response(empty)

            sucursal_ids = user.sucursales_permitidas()
            if not sucursal_ids:
                return Response(empty)

            pedidos_qs = (
                Pedido.objects.filter(
                    empresa=empresa,
                    sucursal_id__in=sucursal_ids,
                    activo=True,
                    detalles__tallas__lleva_reflejante=True,
                )
                .distinct()
                .select_related("cliente", "sucursal")
                .prefetch_related(
                    "detalles",
                    _tallas_ot_prefetch("lleva_reflejante"),
                    "detalles__producto",
                    "detalles__color",
                )
                .order_by("-created_at", "-id")
            )

            operadores_qs = (
                Usuario.objects.filter(empresa=empresa, is_active=True)
                .order_by("first_name", "last_name", "email")
            )

            preview_folio = None
            sucursal_default = getattr(user, "sucursal_default", None)
            if sucursal_default is not None:
                try:
                    from produccion.utils.folios import preview_or_folio

                    preview_folio = preview_or_folio(empresa.pk, sucursal_default.pk)
                except Exception:
                    preview_folio = None

            pedidos_payload = _payload_pedidos_onboarding(
                pedidos_qs,
                "reflejante_config",
                OrdenReflejanteService.cantidades_asignadas_por_pedidos,
                # Sólo reflejante: su config es un ARREGLO, así que los cuatro
                # campos derivados (``posicion_sugerida``/``ubicaciones``/
                # ``foto``/``notas``) salen vacíos y sin esto la línea no
                # llevaría NINGÚN dato de reflejante. Bordado y corte de manga
                # tienen config con forma de objeto y ya lo publican por esa
                # vía, así que no se activa ahí y su respuesta no cambia.
                incluir_config_crudo=True,
            )

            return Response({
                "pedidos": pedidos_payload,
                "operadores": [
                    {"id": u.id, "nombre": u.get_full_name().strip() or u.email}
                    for u in operadores_qs
                ],
                "preview": {"folio_or_sugerido": preview_folio},
            })

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orden_reflejante = OrdenReflejanteService.save(serializer.validated_data, request.user)
        return Response(
            OrdenReflejanteSerializer(
                _con_detalles_prefetcheados(orden_reflejante, OrdenReflejanteDetalle)
            ).data,
            status=status.HTTP_201_CREATED,
        )

class ReflejanteAvancesViewSet(_OrdenPadreTenantScopedMixin, viewsets.ModelViewSet):
    queryset = ReflejanteAvances.objects.filter(activo=True)
    serializer_class = ReflejanteAvancesSerializer
    orden_padre_field = "orden_r"

    def perform_destroy(self, instance):
        instance.soft_delete()

class ReflejanteIncidenciasViewSet(_OrdenPadreTenantScopedMixin, viewsets.ModelViewSet):
    queryset = ReflejanteIncidencias.objects.filter(activo=True)
    serializer_class = ReflejanteIncidenciasSerializer
    orden_padre_field = "orden_r"

    def perform_destroy(self, instance):
        instance.soft_delete()

class OrdenesCorteMangaViewSet(
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
    ):
    queryset = OrdenesCorteManga.objects.all()
    serializer_class = OrdenesCorteMangaSerializer

    def get_queryset(self):
        """Aislamiento multi-tenant: empresa + sucursal.

        Mismo criterio que ``OrdenBordadoViewSet``/``OrdenReflejanteViewSet``.

        ``select_related``/``prefetch_related`` cortan el N+1: el serializer
        resuelve ``pedido`` (``pedido_folio``), ``usuario_asignado``
        (``usuario_nombre``) y ``empresa``/``sucursal``
        (``empresa_nombre``/``sucursal_nombre``) por orden, y por renglón de
        ``detalles``, ``producto``/``talla``/``color``.
        ``OrdenCorteMangaDetalle.configuracion`` es un ``JSONField`` plano (no
        una FK), así que no necesita ``select_related``.
        """
        user = self.request.user
        qs = (
            OrdenesCorteManga.objects.filter(activo=True)
            .select_related("pedido", "usuario_asignado", "empresa", "sucursal")
            .prefetch_related(
                Prefetch(
                    "detalles",
                    # Orden estable local al Prefetch; ver el equivalente en
                    # ``OrdenBordadoViewSet``.
                    queryset=OrdenCorteMangaDetalle.objects.select_related(
                        "producto", "talla", "color"
                    ).order_by("id"),
                )
            )
            # Listado más reciente primero; ``-id`` como desempate estable.
            .order_by("-fecha_inicio", "-id")
        )

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        # Ver ``OrdenBordadoViewSet.get_serializer_class``.
        if self.action == "list":
            return OrdenesCorteMangaListSerializer
        if self.action == "retrieve":
            return OrdenesCorteMangaRetrieveSerializer
        return OrdenesCorteMangaSerializer

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orden_corte_manga = OrdenCorteMangaService.save(serializer.validated_data, request.user)
        return Response(
            OrdenesCorteMangaSerializer(
                _con_detalles_prefetcheados(orden_corte_manga, OrdenCorteMangaDetalle)
            ).data,
            status=status.HTTP_200_OK,
        )

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        """Onboarding para OrdenesCorteManga (patrón WMS picking/packing/despacho).

        GET → catálogos de pedidos con prendas que requieren corte de manga,
        operadores de la empresa, preview del folio siguiente y detalle
        por pedido con líneas elegibles (producto/talla/color, cantidad
        del pedido y ubicaciones/foto de corte_manga_config). El selector
        de cantidades en Next.js usa este detalle para armar el POST.

        POST → mismo save que create() (comparte serializer y service).
        Body opcional `detalles_override[]` permite seleccionar líneas
        y cantidades para OCM parciales.
        """
        if request.method == "GET":
            user = request.user
            empresa = getattr(user, "empresa", None)
            empty = {"pedidos": [], "operadores": [], "preview": {"folio_ocm_sugerido": None}}
            if empresa is None:
                return Response(empty)

            sucursal_ids = user.sucursales_permitidas()
            if not sucursal_ids:
                return Response(empty)

            pedidos_qs = (
                Pedido.objects.filter(
                    empresa=empresa,
                    sucursal_id__in=sucursal_ids,
                    activo=True,
                    detalles__tallas__lleva_corte_manga=True,
                )
                .distinct()
                .select_related("cliente", "sucursal")
                .prefetch_related(
                    "detalles",
                    _tallas_ot_prefetch("lleva_corte_manga"),
                    "detalles__producto",
                    "detalles__color",
                )
                .order_by("-created_at", "-id")
            )

            operadores_qs = (
                Usuario.objects.filter(empresa=empresa, is_active=True)
                .order_by("first_name", "last_name", "email")
            )

            preview_folio = None
            sucursal_default = getattr(user, "sucursal_default", None)
            if sucursal_default is not None:
                try:
                    from produccion.utils.folios import preview_ocm_folio

                    preview_folio = preview_ocm_folio(empresa.pk, sucursal_default.pk)
                except Exception:
                    preview_folio = None

            pedidos_payload = _payload_pedidos_onboarding(
                pedidos_qs,
                "corte_manga_config",
                OrdenCorteMangaService.cantidades_asignadas_por_pedidos,
            )

            return Response({
                "pedidos": pedidos_payload,
                "operadores": [
                    {"id": u.id, "nombre": u.get_full_name().strip() or u.email}
                    for u in operadores_qs
                ],
                "preview": {"folio_ocm_sugerido": preview_folio},
            })

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orden_corte_manga = OrdenCorteMangaService.save(serializer.validated_data, request.user)
        return Response(
            OrdenesCorteMangaSerializer(
                _con_detalles_prefetcheados(orden_corte_manga, OrdenCorteMangaDetalle)
            ).data,
            status=status.HTTP_201_CREATED,
        )