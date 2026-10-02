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
