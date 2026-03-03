from rest_framework import viewsets
from terceros.models import Proveedor
from terceros.api.serializers import ProveedorSerializer

class ProveedorViewSet(viewsets.ModelViewSet):
    queryset = Proveedor.objects.all()
    serializer_class = ProveedorSerializer