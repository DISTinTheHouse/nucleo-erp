from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from planeacion.services import tablero_rotacion as tablero
from ventas.services.pedido_field_filter_service import puede_ver_contabilidad


class TableroRotacionParamsSerializer(serializers.Serializer):
    semanas = serializers.IntegerField(min_value=1, max_value=104, default=12)
    semanas_objetivo = serializers.IntegerField(min_value=1, max_value=52, default=8)
    dias_sin_venta = serializers.IntegerField(min_value=1, max_value=730, default=90)
    agrupar = serializers.ChoiceField(choices=("variante", "producto"), default="variante")
    producto = serializers.IntegerField(min_value=1, required=False)
    categoria = serializers.IntegerField(min_value=1, required=False)
    sucursal = serializers.IntegerField(min_value=1, required=False)
    buscar = serializers.CharField(max_length=100, required=False, allow_blank=True)
    clase = serializers.ChoiceField(choices=("A", "B", "C"), required=False)
    estado = serializers.ChoiceField(choices=tablero.ESTADOS, required=False)
    solo_candidatos = serializers.BooleanField(required=False, default=False)
    ordering = serializers.CharField(required=False, default="-vendido_periodo")
    page = serializers.IntegerField(min_value=1, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=500, default=50)

    def validate_ordering(self, value):
        if value.lstrip("-") not in tablero.ORDENAMIENTOS:
            raise serializers.ValidationError(f"Opciones: {', '.join(sorted(tablero.ORDENAMIENTOS))} (con - para descendente).")
        if value.lstrip("-") == "importe_vendido" and not self.context["ver_importes"]:
            raise serializers.ValidationError("No tienes permiso para ver importes.")
        return value


class TableroRotacionView(APIView):
    """``GET /api/v1/planeacion/tablero-rotacion/``: rotación de producto terminado
    para la junta semanal (ver ``planeacion.services.tablero_rotacion``). Siempre
    sobre la empresa activa del usuario; los importes solo con permiso de importes
    de pedido."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        empresa = getattr(request.user, "empresa", None)
        if empresa is None:
            return Response({"detail": "Tu usuario no tiene una empresa activa."}, status=400)
        ver_importes = puede_ver_contabilidad(request.user)
        # Vacíos = como si no vinieran (mismo criterio que finanzas, EC-760).
        datos = {k: v for k, v in request.query_params.items() if v != ""}
        params = TableroRotacionParamsSerializer(data=datos, context={"ver_importes": ver_importes})
        params.is_valid(raise_exception=True)
        p = params.validated_data

        resultado = tablero.calcular(empresa, p)
        filas = resultado.pop("filas")
        if not ver_importes:
            resultado["resumen"]["importe_vendido"] = None
            for r in filas:
                r["importe_vendido"] = None
        inicio = (p["page"] - 1) * p["page_size"]
        return Response({
            **resultado,
            "count": len(filas),
            "page": p["page"],
            "page_size": p["page_size"],
            "results": filas[inicio:inicio + p["page_size"]],
        })
