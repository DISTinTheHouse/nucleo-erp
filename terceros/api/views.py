from rest_framework import viewsets, status
from terceros.models import Proveedor, Cliente, DireccionCliente
from terceros.api.serializers import ProveedorSerializer, ClienteSerializer, DireccionClienteSerializer
from rest_framework.decorators import action
from rest_framework.response import Response

class ClienteViewSet(viewsets.ModelViewSet):
    queryset = Cliente.objects.all()
    serializer_class = ClienteSerializer
    http_method_names = ["get", "post", "patch"]

class ProveedorViewSet(viewsets.ModelViewSet):
    queryset = Proveedor.objects.all()
    serializer_class = ProveedorSerializer

class DireccionClienteViewSet(viewsets.ModelViewSet):
    queryset = DireccionCliente.objects.all()
    serializer_class = DireccionClienteSerializer
    http_method_names = ["get", "post", "patch"]

    @action(detail=True, methods=['post'])
    def confirmar(self, request, pk=None):
        return Response({'msg': 'DireccionClienteViewSet.confirmar'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post']) 
    def anular(self, request, pk=None):
        return Response({'msg': 'DireccionClienteViewSet.anular'}, status=status.HTTP_200_OK)


