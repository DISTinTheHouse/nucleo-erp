from rest_framework import serializers

from ..models import BitacoraDocumento


class BitacoraDocumentoSerializer(serializers.ModelSerializer):
    usuario_nombre = serializers.SerializerMethodField()
    accion_display = serializers.CharField(source="get_accion_display", read_only=True)

    class Meta:
        model = BitacoraDocumento
        fields = [
            "id", "created_at", "tipo_documento", "documento_id", "folio",
            "accion", "accion_display", "estatus_anterior", "estatus_nuevo",
            "total", "moneda_id", "sucursal_id", "origen_tipo", "origen_id",
            "usuario_id", "usuario_nombre",
        ]

    def get_usuario_nombre(self, obj):
        u = obj.usuario
        if u is None:
            return None
        return f"{u.first_name} {u.last_name}".strip() or u.email
