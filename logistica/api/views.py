from rest_framework import mixins, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from logistica.models import Envio
from nucleo.scope import visibles_en_empresa_activa
from logistica.api.serializers import EnvioSerializer, EnvioCreateSerializer


class EnvioViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    GenericViewSet,
):
    queryset = Envio.objects.all()
    serializer_class = EnvioSerializer

    def get_queryset(self):
        # Empresa activa, superusuario incluido (EC-816/EC-798): el superusuario ya
        # no ve envíos de otras empresas. Dentro de ella, superusuario y admin ven
        # todas las sucursales; el resto, las suyas. Mismo predicado que WMS y
        # producción (``nucleo.scope.visibles_en_empresa_activa``).
        qs = super().get_queryset().select_related("pedido", "transportista", "sucursal")
        return visibles_en_empresa_activa(qs, self.request.user)

    def get_serializer_class(self):
        if self.action == "create":
            return EnvioCreateSerializer
        return EnvioSerializer

    def create(self, request, *args, **kwargs):
        user = request.user
        empresa = getattr(user, "empresa", None)
        if not empresa and not getattr(user, "is_superuser", False):
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pedido = serializer.validated_data["pedido"]

        if empresa and pedido.empresa_id != empresa.pk:
            raise ValidationError({"pedido": "No pertenece a tu empresa."})

        es_staff = getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)
        if not es_staff and pedido.sucursal_id not in user.sucursales_permitidas():
            raise ValidationError({"pedido": "No tienes acceso a la sucursal de este pedido."})

        envio = serializer.save(empresa=pedido.empresa, sucursal=pedido.sucursal)
        return Response(EnvioSerializer(envio).data, status=status.HTTP_201_CREATED)
