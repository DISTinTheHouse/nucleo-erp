from django.db.models import Prefetch, prefetch_related_objects
from django.db import transaction
from rest_framework import status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.viewsets import GenericViewSet
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from ventas.models import Pedido
from usuarios.models import Usuario

from produccion.models import (
    ListaMaterialBom,
    BomDetalle,
    OrdenProduccion,
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
    ConsumoProduccionSerializer,
    ProductoTerminadoEntradasSerializer,
    OrdenBordadoSerializer,
    BordadoAvancesSerializer,
    BordadoIncidenciasSerializer,
    OrdenReflejanteSerializer,
    ReflejanteAvancesSerializer,
    ReflejanteIncidenciasSerializer,
    OrdenesCorteMangaSerializer
)

from produccion.services.orden_bordado_service import OrdenBordadoService
from produccion.services.orden_reflejante_service import OrdenReflejanteService
from produccion.services.orden_produccion_service import OrdenProduccionService
from produccion.services.orden_corte_manga_service import OrdenCorteMangaService


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

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None: return OrdenProduccion.objects.none()
        queryset = OrdenProduccion.objects.filter(empresa=empresa)
        return queryset
    
    def save_op(self, request):
        return self._crear_op_desde_request(request)
    
    def get_op_detalle(self, request):
        op_id = request.query_params.get('op_id', None)
        if op_id is None:
            return Response({'msg': 'No se proporcionó orden de producción'}, status=status.HTTP_400_BAD_REQUEST)
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

class OrdenBordadoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, GenericViewSet):
    queryset = OrdenesBordado.objects.filter()
    serializer_class = OrdenBordadoSerializer

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
                    queryset=OrdenBordadoDetalle.objects.select_related(
                        "producto", "talla", "color"
                    ),
                )
            )
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
                    "detalles__tallas",
                    "detalles__tallas__talla",
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

            pedidos_payload = []
            for p in pedidos_qs:
                lineas = []
                for det in p.detalles.all():
                    for dt in det.tallas.filter(lleva_bordado=True).select_related("talla"):
                        cfg = dt.bordado_config or {}
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
                        lineas.append({
                            "pedido_detalle_talla_id": dt.id,
                            "pedido_detalle_id": det.id,
                            "producto_id": det.producto_id,
                            "producto_nombre": getattr(det.producto, "nombre", None),
                            "talla_id": getattr(dt.talla, "id", None),
                            "talla_nombre": getattr(dt.talla, "nombre", None),
                            "color_id": getattr(det, "color_id", None),
                            "color_nombre": getattr(getattr(det, "color", None), "nombre", None),
                            "cantidad_pedido": float(dt.cantidad or 0),
                            "posicion_sugerida": (
                                cfg.get("posicion")
                                or primera_ubic.get("codigo")
                                or primera_ubic.get("nombre")
                                or None
                            ),
                            "ubicaciones": ubicaciones if isinstance(ubicaciones, list) else [],
                            "foto": foto,
                            "notas": notas,
                        })
                pedidos_payload.append({
                    "id": p.id,
                    "folio": p.folio,
                    "cliente": p.cliente_id,
                    "cliente_nombre": getattr(p.cliente, "nombre", None),
                    "sucursal": p.sucursal_id,
                    "sucursal_nombre": getattr(p.sucursal, "nombre", None),
                    "detalles": lineas,
                })

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
                    queryset=OrdenReflejanteDetalle.objects.select_related(
                        "producto", "talla", "color"
                    ),
                )
            )
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
                    "detalles__tallas",
                    "detalles__tallas__talla",
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

            pedidos_payload = []
            for p in pedidos_qs:
                lineas = []
                for det in p.detalles.all():
                    for dt in det.tallas.filter(lleva_reflejante=True).select_related("talla"):
                        cfg = dt.reflejante_config or {}
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
                        lineas.append({
                            "pedido_detalle_talla_id": dt.id,
                            "pedido_detalle_id": det.id,
                            "producto_id": det.producto_id,
                            "producto_nombre": getattr(det.producto, "nombre", None),
                            "talla_id": getattr(dt.talla, "id", None),
                            "talla_nombre": getattr(dt.talla, "nombre", None),
                            "color_id": getattr(det, "color_id", None),
                            "color_nombre": getattr(getattr(det, "color", None), "nombre", None),
                            "cantidad_pedido": float(dt.cantidad or 0),
                            "posicion_sugerida": (
                                cfg.get("posicion")
                                or primera_ubic.get("codigo")
                                or primera_ubic.get("nombre")
                                or None
                            ),
                            "ubicaciones": ubicaciones if isinstance(ubicaciones, list) else [],
                            "foto": foto,
                            "notas": notas,
                        })
                pedidos_payload.append({
                    "id": p.id,
                    "folio": p.folio,
                    "cliente": p.cliente_id,
                    "cliente_nombre": getattr(p.cliente, "nombre", None),
                    "sucursal": p.sucursal_id,
                    "sucursal_nombre": getattr(p.sucursal, "nombre", None),
                    "detalles": lineas,
                })

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
                    queryset=OrdenCorteMangaDetalle.objects.select_related(
                        "producto", "talla", "color"
                    ),
                )
            )
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
                    "detalles__tallas",
                    "detalles__tallas__talla",
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

            pedidos_payload = []
            for p in pedidos_qs:
                lineas = []
                for det in p.detalles.all():
                    for dt in det.tallas.filter(lleva_corte_manga=True).select_related("talla"):
                        cfg = dt.corte_manga_config or {}
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
                        lineas.append({
                            "pedido_detalle_talla_id": dt.id,
                            "pedido_detalle_id": det.id,
                            "producto_id": det.producto_id,
                            "producto_nombre": getattr(det.producto, "nombre", None),
                            "talla_id": getattr(dt.talla, "id", None),
                            "talla_nombre": getattr(dt.talla, "nombre", None),
                            "color_id": getattr(det, "color_id", None),
                            "color_nombre": getattr(getattr(det, "color", None), "nombre", None),
                            "cantidad_pedido": float(dt.cantidad or 0),
                            "posicion_sugerida": (
                                cfg.get("posicion")
                                or primera_ubic.get("codigo")
                                or primera_ubic.get("nombre")
                                or None
                            ),
                            "ubicaciones": ubicaciones if isinstance(ubicaciones, list) else [],
                            "foto": foto,
                            "notas": notas,
                        })
                pedidos_payload.append({
                    "id": p.id,
                    "folio": p.folio,
                    "cliente": p.cliente_id,
                    "cliente_nombre": getattr(p.cliente, "nombre", None),
                    "sucursal": p.sucursal_id,
                    "sucursal_nombre": getattr(p.sucursal, "nombre", None),
                    "detalles": lineas,
                })

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