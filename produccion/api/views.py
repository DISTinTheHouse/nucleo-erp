from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from produccion.models import (
    ListaMaterialBom, 
    OrdenProduccion, ConsumoProduccion, 
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
    queryset = ListaMaterialBom.objects.all()
    serializer_class = ListaMaterialBomSerializer

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
