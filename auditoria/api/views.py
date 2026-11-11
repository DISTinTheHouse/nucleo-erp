"""Bitácora de documentos: ``GET /api/v1/auditoria/documentos/``.

Solo lectura, empresa activa (superusuario incluido). Para quien no es
superusuario ni admin de empresa, además, solo las sucursales de
``sucursales_permitidas()`` (#1063). ``total`` sale ``null`` si el usuario no
puede ver importes (misma regla que el pedido, EC-784).

Paginación por cursor sobre ``-id`` (sin ``COUNT``), 50 por página (``limit``
hasta 200). Sin ``?ordering=`` ni ``?search=`` (#1064). Filtros (400 si son
inválidos): ``tipo``, ``documento_id``, ``accion``, ``usuario``, ``sucursal``,
``origen_tipo`` + ``origen_id``, ``folio`` (contiene), ``fecha_desde`` /
``fecha_hasta`` (YYYY-MM-DD).

``GET .../documentos/linea-tiempo/?tipo=pedido&documento_id=123`` devuelve
``{"eventos": [...], "truncado": bool}``: la historia del documento y la de todo
lo que deriva de él (OP, OC, recepciones, picking y su transferencia, facturas y
sus cobros, movimientos de inventario...), en orden cronológico; los 500 eventos
más recientes. 404 si el documento no existe en la empresa.
"""
from datetime import date

from django.apps import apps
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response

from nucleo.importes import tiene_claves_de_importes
from nucleo.permisos import permisos_de_la_peticion
from ventas.services.pedido_field_filter_service import CLAVES_IMPORTES_PEDIDO

from ..bitacora import TIPOS
from ..models import BitacoraDocumento
from .serializers import BitacoraDocumentoSerializer, LineaTiempoSerializer

CODIGOS_BITACORA = ("R-CORE", "R-CORE-REPORTES")
MAX_LINEA_TIEMPO = 500
_NIVELES_DERIVADOS = 4


class LecturaBitacora(permissions.BasePermission):
    message = "No tienes permiso para consultar la bitácora de documentos."

    def has_permission(self, request, view):
        return any(c in permisos_de_la_peticion(request) for c in CODIGOS_BITACORA)


class PaginacionBitacora(CursorPagination):
    page_size = 50
    max_page_size = 200
    page_size_query_param = "limit"
    ordering = "-id"

    def get_page_size(self, request):
        valor = request.query_params.get(self.page_size_query_param)
        if valor in (None, ""):
            return self.page_size
        if not (valor.isascii() and valor.isdecimal()) or int(valor) < 1:
            raise ValidationError({"limit": "Debe ser un entero positivo."})
        return min(int(valor), self.max_page_size)


def _entero(params, nombre):
    valor = params.get(nombre)
    if valor in (None, ""):
        return None
    valor = str(valor)
    if not (valor.isascii() and valor.isdecimal()) or len(valor) > 9:
        raise ValidationError({nombre: "Debe ser un número entero."})
    return int(valor)


def _fecha(params, nombre):
    valor = params.get(nombre)
    if not valor:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise ValidationError({nombre: "Usa el formato YYYY-MM-DD."})


def _tipo(params, requerido=False):
    tipo = params.get("tipo")
    if not tipo:
        if requerido:
            raise ValidationError({"tipo": "Es obligatorio."})
        return None
    if tipo not in TIPOS:
        raise ValidationError({"tipo": f"Tipo inválido. Opciones: {', '.join(sorted(TIPOS))}."})
    return tipo


def _alcance(user):
    """Bitácora visible: empresa activa y, para no admins, sus sucursales."""
    empresa_id = getattr(user, "empresa_id", None)
    if not empresa_id:
        return BitacoraDocumento.objects.none()
    qs = BitacoraDocumento.objects.filter(empresa_id=empresa_id)
    if not (user.is_superuser or getattr(user, "is_admin_empresa", False)):
        qs = qs.filter(sucursal_id__in=user.sucursales_permitidas())
    return qs


def _param(nombre, tipo, descripcion, enum=None, requerido=False):
    return OpenApiParameter(nombre, tipo, OpenApiParameter.QUERY, required=requerido, description=descripcion, enum=enum)


_TIPOS_ENUM = sorted(TIPOS)
_FILTROS_LISTADO = [
    _param("tipo", OpenApiTypes.STR, "Tipo de documento.", _TIPOS_ENUM),
    _param("documento_id", OpenApiTypes.INT, "Id del documento."),
    _param("accion", OpenApiTypes.STR, "Acción.", list(BitacoraDocumento.Accion.values)),
    _param("usuario", OpenApiTypes.INT, "Id del usuario."),
    _param("sucursal", OpenApiTypes.INT, "Id de la sucursal."),
    _param("origen_tipo", OpenApiTypes.STR, "Tipo del documento origen.", _TIPOS_ENUM),
    _param("origen_id", OpenApiTypes.INT, "Id del documento origen."),
    _param("folio", OpenApiTypes.STR, "Folio (contiene)."),
    _param("fecha_desde", OpenApiTypes.DATE, "Desde (YYYY-MM-DD)."),
    _param("fecha_hasta", OpenApiTypes.DATE, "Hasta (YYYY-MM-DD)."),
]


@extend_schema_view(list=extend_schema(parameters=_FILTROS_LISTADO))
class BitacoraDocumentoViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BitacoraDocumentoSerializer
    permission_classes = [permissions.IsAuthenticated, LecturaBitacora]
    pagination_class = PaginacionBitacora
    filter_backends = []  # sin ?ordering= / ?search= heredados (#1064)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["ver_importes"] = tiene_claves_de_importes(self.request.user, CLAVES_IMPORTES_PEDIDO)
        return ctx

    def get_queryset(self):
        qs = _alcance(self.request.user).select_related("usuario")
        if self.action != "list":
            return qs
        p = self.request.query_params
        tipo = _tipo(p)
        if tipo:
            qs = qs.filter(tipo_documento=tipo)
        if p.get("documento_id"):
            qs = qs.filter(documento_id=str(_entero(p, "documento_id")))
        accion = p.get("accion")
        if accion:
            if accion not in BitacoraDocumento.Accion.values:
                raise ValidationError({"accion": f"Opciones: {', '.join(BitacoraDocumento.Accion.values)}."})
            qs = qs.filter(accion=accion)
        for param, campo in (("usuario", "usuario_id"), ("sucursal", "sucursal_id"), ("origen_id", "origen_id")):
            valor = _entero(p, param)
            if valor is not None:
                qs = qs.filter(**{campo: valor})
        if p.get("origen_tipo"):
            if p["origen_tipo"] not in TIPOS:
                raise ValidationError({"origen_tipo": "Tipo inválido."})
            qs = qs.filter(origen_tipo=p["origen_tipo"])
        if p.get("folio"):
            qs = qs.filter(folio__icontains=p["folio"])
        desde, hasta = _fecha(p, "fecha_desde"), _fecha(p, "fecha_hasta")
        if desde:
            qs = qs.filter(created_at__date__gte=desde)
        if hasta:
            qs = qs.filter(created_at__date__lte=hasta)
        return qs

    @extend_schema(
        parameters=[
            _param("tipo", OpenApiTypes.STR, "Tipo de documento.", _TIPOS_ENUM, requerido=True),
            _param("documento_id", OpenApiTypes.INT, "Id del documento.", requerido=True),
        ],
        responses=LineaTiempoSerializer,
    )
    @action(detail=False, methods=["get"], url_path="linea-tiempo", pagination_class=None)
    def linea_tiempo(self, request):
        p = request.query_params
        tipo = _tipo(p, requerido=True)
        doc_id = _entero(p, "documento_id")
        if doc_id is None:
            raise ValidationError({"documento_id": "Es obligatorio."})
        user = request.user
        modelo = apps.get_model(TIPOS[tipo])
        if not modelo.objects.filter(pk=doc_id, empresa_id=getattr(user, "empresa_id", None)).exists():
            raise NotFound("Documento no encontrado.")

        base = _alcance(user)
        docs = {(tipo, doc_id)}
        frontera = {(tipo, doc_id)}
        # Derivados por origen, un nivel por consulta (pedido → OC → recepción → ...).
        for _ in range(_NIVELES_DERIVADOS):
            q = Q()
            for t, i in frontera:
                q |= Q(origen_tipo=t, origen_id=i)
            nuevos = {
                (t, int(i)) for t, i in base.filter(q).values_list("tipo_documento", "documento_id").distinct()
            } - docs
            if not nuevos:
                break
            docs |= nuevos
            frontera = nuevos
        docs |= _derivados_sin_origen(docs)

        q = Q()
        for t, i in docs:
            q |= Q(tipo_documento=t, documento_id=str(i))
        filas = list(base.filter(q).select_related("usuario").order_by("-id")[: MAX_LINEA_TIEMPO + 1])
        truncado = len(filas) > MAX_LINEA_TIEMPO
        filas = filas[:MAX_LINEA_TIEMPO][::-1]
        data = BitacoraDocumentoSerializer(filas, many=True, context=self.get_serializer_context()).data
        return Response({"eventos": data, "truncado": truncado})


def _derivados_sin_origen(docs):
    """Documentos ligados que no guardan origen en la bitácora (#1065): la
    transferencia de cada picking y los cobros de cada factura."""
    from finanzas.models import CobroDetalle
    from wms.models import Picking

    extra = set()
    pickings = [i for t, i in docs if t == "picking"]
    if pickings:
        extra |= {
            ("transferencia", tr)
            for tr in Picking.objects.filter(pk__in=pickings, transferencia__isnull=False)
            .values_list("transferencia_id", flat=True)
        }
    facturas = [i for t, i in docs if t == "factura"]
    if facturas:
        extra |= {
            ("cobro", c)
            for c in CobroDetalle.objects.filter(cxc__factura_id__in=facturas)
            .values_list("cobro_id", flat=True).distinct()
        }
    return extra
