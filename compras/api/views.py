from rest_framework import status, viewsets
from compras.models import OrdenCompra, Recepcion
from compras.api.serializers import OrdenCompraSerializer, RecepcionSerializer
from rest_framework.decorators import action
from rest_framework.response import Response

class OrdenCompraViewSet(viewsets.ModelViewSet):
    queryset = OrdenCompra.objects.all()
    serializer_class = OrdenCompraSerializer

    def destroy(self, request, *args, **kwargs):
        # TODO: add destroy logic
        return Response({'msg': 'OrdenCompra deleted'}, status=status.HTTP_200_OK)

class RecepcionViewSet(viewsets.ModelViewSet):
    queryset = Recepcion.objects.all()
    serializer_class = RecepcionSerializer
    http_method_names = ['get', 'post', 'patch']

    @action(detail=True, methods=['post'])
    def confirmar(self, request, pk=None):
        return Response({'msg': 'RecepcionViewSet.confirmar'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post']) 
    def anular(self, request, pk=None):
        return Response({'msg': 'RecepcionViewSet.anular'}, status=status.HTTP_200_OK)
