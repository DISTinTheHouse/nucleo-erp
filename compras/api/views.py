from rest_framework import viewsets
from compras.models import OrdenCompra, Recepcion
from compras.api.serializers import OrdenCompraSerializer, RecepcionSerializer

class OrdenCompraViewSet(viewsets.ModelViewSet):
    queryset = OrdenCompra.objects.all()
    serializer_class = OrdenCompraSerializer

class RecepcionViewSet(viewsets.ModelViewSet):
    queryset = Recepcion.objects.all()
    serializer_class = RecepcionSerializer