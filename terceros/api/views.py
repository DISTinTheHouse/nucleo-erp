from rest_framework import viewsets
from terceros.models import Proveedor, Cliente, DireccionCliente
from terceros.api.serializers import ProveedorSerializer, ClienteSerializer, DireccionClienteSerializer

class ClienteViewSet(viewsets.ModelViewSet):
    queryset = Cliente.objects.filter(activo=True)
    serializer_class = ClienteSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()

class ProveedorViewSet(viewsets.ModelViewSet):
    queryset = Proveedor.objects.filter(activo=True)
    serializer_class = ProveedorSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()

class DireccionClienteViewSet(viewsets.ModelViewSet):
    queryset = DireccionCliente.objects.filter(activo=True)
    serializer_class = DireccionClienteSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()



