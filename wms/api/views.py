from rest_framework.response import Response
from rest_framework import status
from rest_framework.viewsets import GenericViewSet
from wms.api.serializers import TransferenciaSerializer
from wms.services.transferencia_service import TransferenciaService


class TransferenciaViewSet(GenericViewSet):
    serializer_class = TransferenciaSerializer

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        res = TransferenciaService.handle_store(serializer.validated_data, request.user)
        return Response(TransferenciaSerializer(res).data, status=status.HTTP_201_CREATED)
    

