from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from finanzas.models import Factura
from finanzas.api.serializers import FacturaSerializer, FacturaDetalleSerializer
from finanzas.services.factura_service import FacturaService
from terceros.models import Cliente
from terceros.api.serializers import ClienteSerializer

class ClienteViewSet(viewsets.ModelViewSet):
    serializer_class = ClienteSerializer
    http_method_names = ['get']

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None: return Cliente.objects.none()
        queryset = Cliente.objects.filter(empresa=empresa)
        return queryset

class FacturaViewSet(viewsets.ModelViewSet):
    serializer_class = FacturaSerializer
    http_method_names = ['delete', 'get', 'post']

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None: return Factura.objects.none()
        queryset = Factura.objects.filter(empresa=empresa)
        return queryset

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=False, methods=['get', 'post'], url_path='onboarding', url_name='onboarding')
    def onboarding(self, request):
        if request.method == 'GET':
            queryset = self.filter_queryset(self.get_queryset())
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        elif request.method == 'POST':
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            factura = FacturaService.store_factura(request.user, serializer.validated_data)
            serializer = self.get_serializer(factura)
            return Response(serializer.data)
