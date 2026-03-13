from rest_framework import status, viewsets
from compras.models import OrdenCompra, Recepcion
from compras.api.serializers import OrdenCompraSerializer, RecepcionSerializer
from rest_framework.response import Response

class OrdenCompraViewSet(viewsets.ModelViewSet):
    queryset = OrdenCompra.objects.all()
    serializer_class = OrdenCompraSerializer

    def perform_destroy(self, instance):
        instance.soft_delete()

class RecepcionViewSet(viewsets.ModelViewSet):
    queryset = Recepcion.objects.all()
    serializer_class = RecepcionSerializer
    http_method_names = ['get', 'post', 'patch']
