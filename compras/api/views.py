from rest_framework import status, viewsets
from rest_framework.response import Response
from django.utils import timezone
from compras.models import OrdenCompra, Recepcion
from compras.api.serializers import OrdenCompraSerializer, RecepcionSerializer
from nucleo.models import SerieFolio

class OrdenCompraViewSet(viewsets.ModelViewSet):
    queryset = OrdenCompra.objects.filter(activo=True)
    serializer_class = OrdenCompraSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        return qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        # Set empresa and usuario automatically
        fecha_oc = self.request.data.get("fecha_oc") or timezone.now().date()
        instance = serializer.save(empresa=empresa, usuario=user, fecha_oc=fecha_oc)
        # Assign folio if not provided
        if not instance.folio:
            self._asignar_folio_oc(instance, empresa)

    def _asignar_folio_oc(self, instance, empresa):
        # Intentar obtener folio de SerieFolio
        serie_folio = SerieFolio.objects.filter(
            empresa=empresa,
            sucursal=instance.sucursal,
            tipo_documento__iexact="ORDEN_COMPRA",
            activo=True,
        ).order_by("id_serie_folio").first()

        if serie_folio:
            try:
                folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
                serie_folio.folio_actual = nuevo_consecutivo
                serie_folio.ultimo_anio = anio_actual
                serie_folio.save(update_fields=["folio_actual", "ultimo_anio"])
                instance.folio = folio_formateado
                instance.save(update_fields=["folio"])
                return
            except Exception:
                pass
        
        # Fallback si no hay SerieFolio configurada
        count = OrdenCompra.objects.filter(empresa=empresa).count()
        instance.folio = f"OC-{empresa.pk}-{instance.id or count+1}"
        instance.save(update_fields=["folio"])

    def perform_destroy(self, instance):
        instance.soft_delete()

class RecepcionViewSet(viewsets.ModelViewSet):
    queryset = Recepcion.objects.all()
    serializer_class = RecepcionSerializer
    http_method_names = ['get', 'post', 'patch']
