from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from ventas.models import Cotizacion, CotizacionDetalle, Pedido, PedidoDetalle
from ventas.api.serializers import CotizacionSerializer, CotizacionDetalleSerializer, PedidoSerializer, PedidoDetalleSerializer

class CotizacionViewSet(viewsets.ModelViewSet):
    queryset = Cotizacion.objects.all()
    serializer_class = CotizacionSerializer
    http_method_names = ['get', 'post', 'patch']

    def perform_create(self, serializer):
        empresa = self.request.user.empresa 
        serializer.save(empresa=empresa)

class CotizacionDetalleViewSet(viewsets.ModelViewSet):
    queryset = CotizacionDetalle.objects.all()
    serializer_class = CotizacionDetalleSerializer
    http_method_names = ['get', 'post', 'patch']

class PedidoViewSet(viewsets.ModelViewSet):
    queryset = Pedido.objects.filter(activo=True)
    serializer_class = PedidoSerializer

    def perform_create(self, serializer):
        empresa = self.request.user.empresa
        serializer.save(empresa=empresa)
    
    def perform_destroy(self, instance):
        instance.soft_delete()
    
class PedidoDetalleViewSet(viewsets.ModelViewSet):
    queryset = PedidoDetalle.objects.all()
    serializer_class = PedidoDetalleSerializer
    http_method_names = ['get', 'post', 'patch']

    @action(detail=True, methods=['post'])
    def anular(self, request, pk=None):
        return Response({"msg": "PedidoDetalleViewSet.anular"})