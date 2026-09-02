from rest_framework import serializers

from ..models import Notificacion


class NotificacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notificacion
        fields = [
            "id",
            "titulo",
            "mensaje",
            "modulo",
            "tipo",
            "leido",
            "leido_at",
            "data",
            "created_at",
        ]
        read_only_fields = fields
