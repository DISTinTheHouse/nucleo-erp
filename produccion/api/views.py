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
    OrdenProduccionSerializer, 
    ConsumoProduccionSerializer, 
    ProductoTerminadoEntradasSerializer,
    OrdenBordadoSerializer,
    BordadoAvancesSerializer,
    BordadoIncidenciasSerializer,
    OrdenReflejanteSerializer,
    ReflejanteAvancesSerializer,
    ReflejanteIncidenciasSerializer,
)

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
        
            if not ListaMaterialBom.objects.filter(producto_variante_id=producto_variante_id).exists():
                raise ValidationError({"producto_variante_id": "There is no list of materials for the specified product variant."})

            queryset = queryset.filter(producto_variante_id=producto_variante_id)
            
        return queryset

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
    http_method_names = ['get', 'post', 'patch']

    @action(detail=True, methods=['POST'])
    def confirmar(self, request, pk=None):
        return Response({'msg': 'OrdenProduccionViewSet.confirmar'}, status=status.HTTP_200_OK)
    
    @action(detail=True, methods=['POST'])
    def anular(self, request, pk=None):
        return Response({'msg': 'OrdenProduccionViewSet.anular'}, status=status.HTTP_200_OK)

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
