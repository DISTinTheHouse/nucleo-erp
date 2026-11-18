from rest_framework import serializers
from logistica.models import Envio


class EnvioSerializer(serializers.ModelSerializer):
    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True)

    class Meta:
        model = Envio
        fields = "__all__"


class EnvioCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Envio
        fields = ["pedido", "transportista"]

    def get_fields(self):
        """``pedido`` y ``transportista`` solo de la empresa activa: un id de otra
        empresa responde igual que uno inexistente (#510, EC-916)."""
        from terceros.models import Transportista
        from ventas.models import Pedido

        fields = super().get_fields()
        user = getattr(self.context.get("request"), "user", None)
        empresa_id = getattr(user, "empresa_id", None)
        fields["pedido"].queryset = Pedido.objects.filter(empresa_id=empresa_id)
        fields["transportista"].queryset = Transportista.objects.filter(empresa_id=empresa_id)
        return fields
