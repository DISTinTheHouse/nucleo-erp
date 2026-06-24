from django.db import transaction

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

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
)

from produccion.api.serializers import (
    ListaMaterialBomSerializer,
    BomDetalleSerializer,
    BomBulkItemSerializer,
    OrdenProduccionSerializer,
    OrdenProduccionOnboardingSerializer,
    ConsumoProduccionSerializer,
    ProductoTerminadoEntradasSerializer,
    OrdenBordadoSerializer,
    BordadoAvancesSerializer,
    BordadoIncidenciasSerializer,
    OrdenReflejanteSerializer,
    ReflejanteAvancesSerializer,
    ReflejanteIncidenciasSerializer,
)

from produccion.services.orden_produccion_service import OrdenProduccionService

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
        # Nuevo contrato: el cliente sólo envía `producto_variante_ids`. El
        # `bom`, la `cantidad`, la `unidad` y las `observaciones` de cada
        # detalle se resuelven en el servidor a partir de los `bom_detalle`
        # del BOM activo de cada variante.
        input_serializer = OrdenProduccionOnboardingSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        producto_variante_ids = input_serializer.validated_data['producto_variante_ids']

        with transaction.atomic():
            # 1. Resolver el BOM activo de cada variante y armar el detalle a
            #    partir de sus `bom_detalle` (un detalle por componente).
            detalle_rows = []
            variantes_sin_bom = []
            for producto_variante_id in producto_variante_ids:
                try:
                    bom = ListaMaterialBom.objects.get(
                        producto_variante_id=producto_variante_id,
                        activo=True,
                        empresa=request.user.empresa,
                    )
                except ListaMaterialBom.DoesNotExist:
                    variantes_sin_bom.append(producto_variante_id)
                    continue

                for bom_detalle in bom.materia_prima_detalle.filter(activo=True):
                    detalle_rows.append({
                        'bom': bom.bom_id,
                        'producto_variante_id': producto_variante_id,
                        'cantidad': bom_detalle.cantidad,
                        'unidad': bom_detalle.unidad_id,
                        'observaciones': bom_detalle.observaciones,
                    })

            # 2. Si alguna variante no tiene BOM activo -> 400 (no 500).
            if variantes_sin_bom:
                return Response(
                    {
                        'msg': 'No existe una lista de materiales (BOM) activa para las variantes indicadas.',
                        'producto_variante_ids_sin_bom': variantes_sin_bom,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # 3. Reutilizar el resto del cuerpo (empresa, sucursal, etc.) e
            #    inyectar el detalle resuelto; la creación sigue idéntica.
            op_data = {
                key: value
                for key, value in request.data.items()
                if key != 'producto_variante_ids'
            }
            op_data['orden_produccion_detalle'] = detalle_rows

            serializer = self.get_serializer(data=op_data)
            serializer.is_valid(raise_exception=True)
            OrdenProduccionService.save_orden_produccion(serializer.validated_data, request.user)

        return Response({'msg': 'Orden de producción creada exitosamente'}, status=status.HTTP_201_CREATED)
    
    def get_op_detalle(self, request):
        op_id = request.query_params.get('op_id', None)
        if op_id is None:
            return Response({'msg': 'No se proporcionó orden de producción'}, status=status.HTTP_400_BAD_REQUEST)
        res_data = OrdenProduccionService.get_formatted_op_detalle(op_id)
        if res_data is None:
            return Response({'msg': 'Orden de producción no encontrada'}, status=status.HTTP_404_NOT_FOUND)
        return Response(res_data)
    
    @action(detail=False, methods=['get', 'post'], url_path='onboarding')
    def onboarding(self, request):
        if request.method == 'GET':
            op_id = request.query_params.get('op_id', None)
            return self.get_op_detalle(request)
        elif request.method == 'POST':
            return self.save_op(request)

class ConsumoProduccionViewSet(viewsets.ModelViewSet):
    queryset = ConsumoProduccion.objects.all()
    serializer_class = ConsumoProduccionSerializer
    http_method_names = ['get', 'post']

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

class OrdenBordadoViewSet(viewsets.ModelViewSet):
    queryset = OrdenesBordado.objects.filter(activo=True)
    serializer_class = OrdenBordadoSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()

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

class OrdenReflejanteViewSet(viewsets.ModelViewSet):
    queryset = OrdenesReflejante.objects.filter(activo=True)
    serializer_class = OrdenReflejanteSerializer

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
