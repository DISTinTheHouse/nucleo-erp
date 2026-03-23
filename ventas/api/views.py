from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from ventas.models import Cotizacion, CotizacionDetalle, Pedido, PedidoDetalle, PedidoDetalleTalla
from ventas.api.serializers import CotizacionSerializer, CotizacionDetalleSerializer, PedidoSerializer, PedidoDetalleSerializer, PedidoDetalleTallaSerializer
from nucleo.models import SerieFolio

class CotizacionViewSet(viewsets.ModelViewSet):
    queryset = Cotizacion.objects.all()
    serializer_class = CotizacionSerializer
    http_method_names = ['get', 'post', 'patch']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        return qs.none()

    def perform_create(self, serializer):
        empresa = self.request.user.empresa 
        serializer.save(empresa=empresa)

class CotizacionDetalleViewSet(viewsets.ModelViewSet):
    queryset = CotizacionDetalle.objects.all()
    serializer_class = CotizacionDetalleSerializer
    http_method_names = ['get', 'post', 'patch']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(cotizacion__empresa=empresa)
        return qs.none()

class PedidoViewSet(viewsets.ModelViewSet):
    queryset = Pedido.objects.filter(activo=True)
    serializer_class = PedidoSerializer

    def perform_create(self, serializer):
        empresa = self.request.user.empresa
        with transaction.atomic():
            pedido = serializer.save(empresa=empresa)

            if pedido.folio:
                return

            serie_folio = None

            if pedido.serie_folio_id:
                serie_folio = SerieFolio.objects.select_for_update().filter(
                    pk=pedido.serie_folio_id
                ).first()
            else:
                serie_folio = SerieFolio.objects.select_for_update().filter(
                    empresa=empresa,
                    sucursal=pedido.sucursal,
                    tipo_documento__iexact="PEDIDO",
                    activo=True,
                ).order_by("id_serie_folio").first()

            if not serie_folio:
                return

            folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()

            serie_folio.folio_actual = nuevo_consecutivo
            serie_folio.ultimo_anio = anio_actual
            serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])

            pedido.serie_folio = serie_folio
            pedido.folio = folio_formateado
            pedido.folio_consecutivo = nuevo_consecutivo
            pedido.folio_anio = anio_actual
            pedido.save(update_fields=["serie_folio", "folio", "folio_consecutivo", "folio_anio"])
    
    def perform_destroy(self, instance):
        instance.soft_delete()
    
class PedidoDetalleViewSet(viewsets.ModelViewSet):
    queryset = PedidoDetalle.objects.all()
    serializer_class = PedidoDetalleSerializer
    http_method_names = ['get', 'post', 'patch']

    @action(detail=True, methods=['post'])
    def anular(self, request, pk=None):
        return Response({"msg": "PedidoDetalleViewSet.anular"})

class PedidoDetalleTallaViewSet(viewsets.ModelViewSet):
    queryset = PedidoDetalleTalla.objects.all()
    serializer_class = PedidoDetalleTallaSerializer
    http_method_names = ['get', 'post', 'patch']
