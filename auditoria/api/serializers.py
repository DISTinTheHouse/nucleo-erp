from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from ..models import BitacoraDocumento


class BitacoraDocumentoSerializer(serializers.ModelSerializer):
    usuario_nombre = serializers.SerializerMethodField()
    accion_display = serializers.CharField(source="get_accion_display", read_only=True)
    total = serializers.SerializerMethodField()

    class Meta:
        model = BitacoraDocumento
        fields = [
            "id", "created_at", "tipo_documento", "documento_id", "folio",
            "accion", "accion_display", "estatus_anterior", "estatus_nuevo", "estatus_codigo",
            "total", "moneda_id", "sucursal_id", "origen_tipo", "origen_id",
            "usuario_id", "usuario_nombre",
        ]

    @extend_schema_field({"type": "string", "format": "decimal", "nullable": True})
    def get_total(self, obj):
        # Sin permiso de importes (EC-784), ``null`` (#1063).
        if not self.context.get("ver_importes", False) or obj.total is None:
            return None
        return str(obj.total)

    @extend_schema_field({"type": "string", "nullable": True})
    def get_usuario_nombre(self, obj):
        u = obj.usuario
        if u is None:
            return None
        return f"{u.first_name} {u.last_name}".strip() or u.email


class LineaTiempoSerializer(serializers.Serializer):
    """Respuesta de ``linea-tiempo`` (solo para el schema)."""

    eventos = BitacoraDocumentoSerializer(many=True)
    truncado = serializers.BooleanField()
