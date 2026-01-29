from rest_framework import viewsets
from .models import Empresa
from .serializers import EmpresaSerializer

class EmpresaViewSet(viewsets.ModelViewSet):
    """
    API endpoint para ver y editar empresas.
    """
    queryset = Empresa.objects.all()
    serializer_class = EmpresaSerializer
    lookup_field = 'codigo' # Permitir acceder por código (ej: /api/empresas/DEMO/)
