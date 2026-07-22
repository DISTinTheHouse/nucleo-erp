from rest_framework import mixins
from rest_framework.response import Response
from rest_framework import status
from rest_framework.viewsets import GenericViewSet
from wms.models import Picking
from wms.api.serializers import TransferenciaSerializer, PickingSerializer
from wms.services.transferencia_service import TransferenciaService
from wms.services.picking_service import PickingService

class TransferenciaViewSet(GenericViewSet):
    serializer_class = TransferenciaSerializer

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            res = TransferenciaService.handle_store(serializer.validated_data, request.user)
            return Response(TransferenciaSerializer(res).data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({"err": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    
class PickingViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, GenericViewSet):
    queryset = Picking.objects.all()
    serializer_class = PickingSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        empresa = getattr(user, "empresa", None)
        if not empresa: return qs.none()
        return qs.filter(empresa=empresa)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        res = PickingService.handle_store(serializer.validated_data, request.user)
        return Response(PickingSerializer(res).data, status=status.HTTP_201_CREATED)