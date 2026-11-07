"""Costo promedio vigente por artículo y su historial de cambios (#809)."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from inventarios.models import CostoInventario, HistorialCostoInventario
from nucleo.kpis import entero_param
from nucleo.permisos import permisos_efectivos


class CostoInventarioSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()

    class Meta:
        model = CostoInventario
        fields = [
            "id", "empresa", "producto", "producto_variante", "variante_produccion",
            "producto_nombre", "sku", "costo_promedio", "actualizado_en",
        ]

    def get_producto_nombre(self, obj):
        producto = obj.producto or getattr(obj.producto_variante, "producto", None)
        return getattr(producto, "nombre", None) or getattr(obj.variante_produccion, "nombre", None)

    def get_sku(self, obj):
        return getattr(obj.producto_variante, "sku", None) or getattr(obj.variante_produccion, "sku", None)


class HistorialCostoInventarioSerializer(serializers.ModelSerializer):
    movimiento = serializers.IntegerField(source="movimiento_detalle.movimiento_inventario_id", read_only=True, default=None)
    diferencia = serializers.SerializerMethodField()

    class Meta:
        model = HistorialCostoInventario
        fields = [
            "id", "fecha", "origen", "costo_anterior", "costo_nuevo", "diferencia",
            "existencia_previa", "cantidad_entrada", "costo_entrada", "movimiento_detalle", "movimiento",
        ]

    def get_diferencia(self, obj):
        return str(obj.costo_nuevo - obj.costo_anterior)


class CostoInventarioViewSet(viewsets.ReadOnlyModelViewSet):
    """Solo lectura. Exige ``R-CONTABILIDAD`` (superusuario y admin de empresa pasan): sin él, 403."""

    serializer_class = CostoInventarioSerializer
    queryset = CostoInventario.objects.select_related(
        "producto", "producto_variante__producto", "variante_produccion",
    ).order_by("-actualizado_en", "-id")

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if "R-CONTABILIDAD" not in permisos_efectivos(request.user):
            raise PermissionDenied("No tienes permiso para ver costos de inventario.")

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if not user.is_superuser:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        if self.action == "list":
            for nombre in ("producto", "producto_variante", "variante_produccion"):
                valor = entero_param(self.request, nombre)
                if valor is not None:
                    qs = qs.filter(**{f"{nombre}_id": valor})
        return qs

    @extend_schema(parameters=[
        OpenApiParameter("producto", OpenApiTypes.INT), OpenApiParameter("producto_variante", OpenApiTypes.INT),
        OpenApiParameter("variante_produccion", OpenApiTypes.INT),
    ])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(responses=HistorialCostoInventarioSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="historial")
    def historial(self, request, pk=None):
        """Cambios del costo promedio, el más reciente primero (paginado con ``limit``/``offset``)."""
        qs = HistorialCostoInventario.objects.filter(costo_inventario=self.get_object()).select_related(
            "movimiento_detalle"
        )
        pagina = self.paginate_queryset(qs)
        if pagina is not None:
            return self.get_paginated_response(HistorialCostoInventarioSerializer(pagina, many=True).data)
        return self.get_paginated_response(HistorialCostoInventarioSerializer(qs, many=True).data) if False else \
            __import__("rest_framework.response", fromlist=["Response"]).Response(
                HistorialCostoInventarioSerializer(qs, many=True).data
            )
