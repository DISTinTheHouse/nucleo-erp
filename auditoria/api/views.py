"""Bitácora de documentos: ``GET /api/v1/auditoria/documentos/``.

Solo lectura, empresa activa (superusuario incluido). Paginación por cursor
(sin ``COUNT``), 50 por página (``limit`` hasta 200). Filtros: ``tipo``,
``documento_id``, ``accion``, ``usuario``, ``sucursal``, ``origen_tipo`` +
``origen_id``, ``folio`` (contiene), ``fecha_desde`` / ``fecha_hasta`` (YYYY-MM-DD).

``GET .../documentos/linea-tiempo/?tipo=pedido&documento_id=123`` devuelve la
historia de un documento y la de sus derivados (los que lo tienen como origen),
en orden cronológico.
"""
from django.db.models import Q
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from nucleo.permisos import permisos_de_la_peticion

from ..models import BitacoraDocumento
from .serializers import BitacoraDocumentoSerializer

CODIGOS_BITACORA = ("R-CORE", "R-CORE-REPORTES")


class LecturaBitacora(permissions.BasePermission):
    message = "No tienes permiso para consultar la bitácora de documentos."

    def has_permission(self, request, view):
        return any(c in permisos_de_la_peticion(request) for c in CODIGOS_BITACORA)


class PaginacionBitacora(CursorPagination):
    page_size = 50
    max_page_size = 200
    page_size_query_param = "limit"
    ordering = "-id"


class BitacoraDocumentoViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BitacoraDocumentoSerializer
    permission_classes = [permissions.IsAuthenticated, LecturaBitacora]
    pagination_class = PaginacionBitacora

    def get_queryset(self):
        empresa_id = getattr(self.request.user, "empresa_id", None)
        if not empresa_id:
            return BitacoraDocumento.objects.none()
        qs = BitacoraDocumento.objects.filter(empresa_id=empresa_id).select_related("usuario")
        p = self.request.query_params
        filtros = {
            "tipo": "tipo_documento", "documento_id": "documento_id", "accion": "accion",
            "usuario": "usuario_id", "sucursal": "sucursal_id",
            "origen_tipo": "origen_tipo", "origen_id": "origen_id",
        }
        for param, campo in filtros.items():
            if p.get(param):
                qs = qs.filter(**{campo: p[param]})
        if p.get("folio"):
            qs = qs.filter(folio__icontains=p["folio"])
        if p.get("fecha_desde"):
            qs = qs.filter(created_at__date__gte=p["fecha_desde"])
        if p.get("fecha_hasta"):
            qs = qs.filter(created_at__date__lte=p["fecha_hasta"])
        return qs

    @action(detail=False, methods=["get"], url_path="linea-tiempo")
    def linea_tiempo(self, request):
        tipo = request.query_params.get("tipo")
        doc = request.query_params.get("documento_id")
        if not tipo or not doc or not str(doc).isdigit():
            raise ValidationError({"detail": "Se requieren tipo y documento_id numérico."})
        empresa_id = getattr(request.user, "empresa_id", None)
        qs = (
            BitacoraDocumento.objects.filter(empresa_id=empresa_id)
            .filter(Q(tipo_documento=tipo, documento_id=doc) | Q(origen_tipo=tipo, origen_id=int(doc)))
            .select_related("usuario")
            .order_by("id")[:500]
        )
        return Response(BitacoraDocumentoSerializer(qs, many=True).data)
