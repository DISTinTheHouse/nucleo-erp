from django.db.models import Prefetch
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
    BordadoAvances,
    BordadoIncidencias,
    OrdenesReflejante,
    ReflejanteAvances,
    ReflejanteIncidencias,
    OrdenesCorteManga
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
        """
        user = self.request.user
        qs = OrdenesBordado.objects.filter(activo=True)

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
        return Response(OrdenBordadoSerializer(orden_bordado).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        """Onboarding para OrdenBordado (patrón WMS picking/packing/despacho).

        GET → catálogos de pedidos con prendas que requieren bordado,
        operadores de la empresa y preview del folio siguiente.

        POST → mismo save que create() (comparte serializer y service).
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

            return Response({
                "pedidos": [
                    {
                        "id": p.id,
                        "folio": p.folio,
                        "cliente": p.cliente_id,
                        "cliente_nombre": getattr(p.cliente, "nombre", None),
                        "sucursal": p.sucursal_id,
                        "sucursal_nombre": getattr(p.sucursal, "nombre", None),
                    }
                    for p in pedidos_qs
                ],
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
            OrdenBordadoSerializer(orden_bordado).data, status=status.HTTP_201_CREATED
        )

class BordadoAvancesViewSet(viewsets.ModelViewSet):
    queryset = BordadoAvances.objects.filter(activo=True)
    serializer_class = BordadoAvancesSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()

class BordadoIncidenciasViewSet(viewsets.ModelViewSet):
    queryset = BordadoIncidencias.objects.filter(activo=True)
    serializer_class = BordadoIncidenciasSerializer

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
        user = self.request.user
        qs = OrdenesReflejante.objects.filter(activo=True)

        if getattr(user, "is_superuser", False): return qs

        empresa = getattr(user, "empresa", None)
        if not empresa: return qs.none()
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return qs

        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orden_reflejante = OrdenReflejanteService.save(serializer.validated_data, request.user)
        return Response(OrdenReflejanteSerializer(orden_reflejante).data, status=status.HTTP_200_OK)

    def perform_destroy(self, instance):
        instance.soft_delete()

class ReflejanteAvancesViewSet(viewsets.ModelViewSet):
    queryset = ReflejanteAvances.objects.filter(activo=True)
    serializer_class = ReflejanteAvancesSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()

class ReflejanteIncidenciasViewSet(viewsets.ModelViewSet):
    queryset = ReflejanteIncidencias.objects.filter(activo=True)
    serializer_class = ReflejanteIncidenciasSerializer

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
        user = self.request.user
        qs = OrdenesCorteManga.objects.filter(activo=True)
        if getattr(user, "is_superuser", False): return qs

        empresa = getattr(user, "empresa", False)
        if not empresa: return qs.none()
        qs = qs.filter(empresa=empresa)

        if getattr(user, "is_admin_empresa", False):
            return qs

        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orden_corte_manga = OrdenCorteMangaService.save(serializer.validated_data, request.user)
        return Response(OrdenesCorteMangaSerializer(orden_corte_manga).data, status=status.HTTP_200_OK)

    def perform_destroy(self, instance):
        instance.soft_delete()