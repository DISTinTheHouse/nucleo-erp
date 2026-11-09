from nucleo.scope import empresa_ids_lectura
from nucleo.middleware import ip_auditoria
from nucleo.permisos import permisos_efectivos
from collections import defaultdict
from decimal import Decimal

from django.db import models, transaction
from django.db.models import ExpressionWrapper, F, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status, viewsets, permissions
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.renderers import BaseRenderer, JSONRenderer
from rest_framework.response import Response

from inventarios.services import costeo
from inventarios.services import reportes_exportacion as exportacion_mod
from inventarios.models import (
    Almacen,
    TipoAlmacen,
    Ubicacion,
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    AjusteInventario,
)
from catalogo.models import Producto, ProductoVariante, VarianteProductoProduccion
from auditoria.models import AuditoriaEvento
from nucleo.models import Sucursal
from ventas.models import Pedido
from nucleo.listados import PaginacionOpcionalLimitOffset
from . import auxiliar as auxiliar_mod
from . import existencias_por_almacen as por_almacen_mod
from . import reportes_periodo as reportes_mod
from .reportes_periodo import almacenes_en_alcance
from .serializers import (
    AlmacenSerializer,
    UbicacionSerializer,
    ExistenciaSerializer,
    ExistenciaPorAlmacenSerializer,
    MovimientoInventarioSerializer,
    MovimientoInventarioDetalleSerializer,
    AjusteInventarioSerializer,
    AuditoriaMovimientoSerializer,
)

class IsAuthenticatedAndScoped(permissions.BasePermission):
    """
    - Permite lectura a autenticados.
    - Crea/edita solo si es admin de empresa o superuser.
    - Elimina solo si es admin de empresa o superuser.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        # Write operations: require admin_empresa or superuser
        return request.user.is_superuser or getattr(request.user, 'is_admin_empresa', False)

    def has_object_permission(self, request, view, obj):
        # Lectura ya pasó por has_permission
        if request.method in permissions.SAFE_METHODS:
            return True
        # Para write/delete, mismo criterio: admin_empresa o superuser
        return request.user.is_superuser or getattr(request.user, 'is_admin_empresa', False)


PERMISO_REPORTES = "R-CORE-REPORTES"


class PermisoReportesInventario(permissions.BasePermission):
    """Reportes por periodo de existencias y movimientos (JSON y ``exportar/``,
    EC-822): exige ``R-CORE-REPORTES``, el mismo código con el que el front abre
    ``/system/reports``. Superusuario y admin de empresa pasan siempre
    (``permisos_efectivos``); ``R-CORE`` solo no basta."""

    message = "No tienes permiso para ver los reportes de inventario."

    def has_permission(self, request, view):
        return PERMISO_REPORTES in permisos_efectivos(request.user)


# El ``permission_classes`` de una acción reemplaza el del ViewSet: se repite
# ``IsAuthenticatedAndScoped`` para que sin sesión siga siendo 401, no 403.
PERMISOS_REPORTES = [IsAuthenticatedAndScoped, PermisoReportesInventario]


class AlmacenViewSet(viewsets.ModelViewSet):
    queryset = Almacen.objects.all().select_related('empresa', 'sucursal')
    serializer_class = AlmacenSerializer
    permission_classes = [IsAuthenticatedAndScoped]
    lookup_field = 'id_almacen'

    def get_queryset(self):
        user = self.request.user
        # Listado más reciente primero; el desempate usa la PK real (``id_almacen``).
        qs = self.queryset.order_by(models.F('created_at').desc(nulls_last=True), '-id_almacen')
        if user.is_superuser:
            return qs
        # Filtrar por empresas y sucursales permitidas
        empresa_ids = empresa_ids_lectura(user)  # solo la empresa activa (EC-816)
        sucursal_ids = list(user.sucursales_permitidas())
        return qs.filter(
            models.Q(empresa_id__in=empresa_ids) &
            models.Q(sucursal_id__in=sucursal_ids)
        ).distinct().order_by(models.F('created_at').desc(nulls_last=True), '-id_almacen')

    def perform_create(self, serializer):
        user = self.request.user
        instance = serializer.save()
        # Verificación final de alcance: empresa/sucursal dentro del scope del usuario
        if not user.is_superuser:
            if instance.sucursal_id and not user.sucursales.filter(pk=instance.sucursal_id).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            if instance.empresa_id:
                if instance.empresa_id != user.empresa_id:  # sin empresa activa no se escribe (EC-816)
                    raise PermissionDenied("No tiene acceso a esta empresa")

class UbicacionViewSet(viewsets.ModelViewSet):
    queryset = Ubicacion.objects.all().select_related('almacen__empresa', 'almacen__sucursal')
    serializer_class = UbicacionSerializer
    permission_classes = [IsAuthenticatedAndScoped]
    lookup_field = 'id_ubicacion'

    def get_queryset(self):
        user = self.request.user
        # Listado más reciente primero; el desempate usa la PK real (``id_ubicacion``).
        qs = self.queryset.order_by(models.F('created_at').desc(nulls_last=True), '-id_ubicacion')
        if user.is_superuser:
            return qs
        empresa_ids = empresa_ids_lectura(user)  # solo la empresa activa (EC-816)
        sucursal_ids = list(user.sucursales_permitidas())
        return qs.filter(
            models.Q(almacen__empresa_id__in=empresa_ids) &
            models.Q(almacen__sucursal_id__in=sucursal_ids)
        ).distinct().order_by(models.F('created_at').desc(nulls_last=True), '-id_ubicacion')

    def perform_create(self, serializer):
        user = self.request.user
        # Obtener el almacén del validated_data
        almacen = serializer.validated_data.get('almacen')
        
        if not user.is_superuser and almacen:
            sucursal = almacen.sucursal
            empresa = almacen.empresa
            
            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if empresa.pk != user.empresa_id:  # sin empresa activa no se escribe (EC-816)
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()

# --- Exportación de los reportes por periodo (EC-814) ---------------------------

PARAMETRO_FORMATO_EXPORTACION = OpenApiParameter(
    "formato", OpenApiTypes.STR, enum=list(exportacion_mod.FORMATOS), description="pdf (por defecto) o xlsx.",
)
PARAMETROS_FECHAS_REPORTE = [
    OpenApiParameter("fecha_inicio", OpenApiTypes.DATE, required=True, description="YYYY-MM-DD."),
    OpenApiParameter("fecha_final", OpenApiTypes.DATE, required=True, description="YYYY-MM-DD."),
]
RESPUESTAS_EXPORTACION = {
    (200, exportacion_mod.CONTENT_TYPES["pdf"]): OpenApiTypes.BINARY,
    (200, exportacion_mod.CONTENT_TYPES["xlsx"]): OpenApiTypes.BINARY,
    400: OpenApiResponse(description="Validación (formato, fechas, tipo_movimiento)."),
    403: OpenApiResponse(description="Sin R-CORE-REPORTES."),
}


class _RendererArchivo(BaseRenderer):
    """Solo para que la negociación de contenido acepte ``Accept`` con el tipo
    del archivo: la acción responde con un ``HttpResponse`` ya armado, que DRF
    no vuelve a renderizar. Los errores se van a JSON en ``finalize_response``
    de ``ExportacionArchivoMixin``."""

    charset = None
    render_style = "binary"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return data


class RendererPdf(_RendererArchivo):
    media_type = exportacion_mod.CONTENT_TYPES["pdf"]
    format = "pdf"


class RendererXlsx(_RendererArchivo):
    media_type = exportacion_mod.CONTENT_TYPES["xlsx"]
    format = "xlsx"


# JSON primero: con ``*/*`` o sin ``Accept`` gana JSON (los 400 salen igual).
RENDERERS_EXPORTACION = [JSONRenderer, RendererPdf, RendererXlsx]


class ExportacionArchivoMixin:
    """Un 400/403/404 de una acción de exportación sale en JSON aunque el
    cliente haya pedido ``Accept: application/pdf`` (o el tipo del XLSX)."""

    def finalize_response(self, request, response, *args, **kwargs):
        if isinstance(response, Response) and isinstance(
            getattr(request, "accepted_renderer", None), _RendererArchivo
        ):
            request.accepted_renderer = JSONRenderer()
            request.accepted_media_type = JSONRenderer.media_type
        return super().finalize_response(request, response, *args, **kwargs)


def respuesta_archivo(exportacion):
    """Descarga (``attachment``) con el nombre del reporte y su rango de fechas,
    como el ``pdf-fusionado/`` de facturas de proveedor."""
    response = HttpResponse(exportacion.contenido, content_type=exportacion.content_type)
    response["Content-Disposition"] = f'attachment; filename="{exportacion.nombre_archivo}"'
    return response


class ReporteExistenciasPeriodoPagination(PageNumberPagination):
    # Instantiated explicitly inside the action, not set as pagination_class
    # on the ViewSet, so list()/other actions are unaffected.
    page_size = 200
    page_size_query_param = "page_size"


class ExistenciaViewSet(ExportacionArchivoMixin, viewsets.ModelViewSet):
    queryset = Existencia.objects.all().select_related(
        "producto",
        "producto__tipo",
        "producto_variante__producto",
        "producto_variante__producto__tipo",
        "producto_variante__color",
        "producto_variante__talla",
        "variante_produccion__color",
        "variante_produccion__talla",
        "almacen",
        "ubicacion",
        "ubicacion__almacen",
    )
    serializer_class = ExistenciaSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def _almacenes_en_alcance(self):
        # ``Existencia`` no tiene ``empresa`` propia: la hereda del almacén.
        return almacenes_en_alcance(self.request.user)

    def get_queryset(self):
        def to_int(v):
            if v in (None, ""): return None
            try:
                return int(v)
            except Exception:
                return None

        qs = self.queryset
        # Aislamiento multi-tenant ANTES de los filtros del cliente y del recorte
        # por ``limit``: ningún query param (``empresa_id``, ``almacen_id``...)
        # abre el alcance. Superusuario ve todo.
        if not self.request.user.is_superuser:
            qs = qs.filter(almacen__in=self._almacenes_en_alcance())
        qp = self.request.query_params

        empresa_id = to_int(qp.get("empresa_id") or qp.get("empresa"))
        sucursal_id = to_int(qp.get("sucursal_id") or qp.get("sucursal"))
        almacen_id = to_int(qp.get("almacen_id") or qp.get("almacen"))
        producto_variante_id = to_int(qp.get("producto_variante_id") or qp.get("producto_variante"))
        producto_id = to_int(qp.get("producto_id") or qp.get("producto"))
        color_id = to_int(qp.get("color_id") or qp.get("color"))
        talla_id = to_int(qp.get("talla_id") or qp.get("talla"))
        sku_q = (qp.get("sku") or qp.get("q") or "").strip()
        almacen_id = to_int(qp.get("almacen_id") or qp.get("almacen"))

        if empresa_id: qs = qs.filter(almacen__empresa_id=empresa_id)
        if sucursal_id: qs = qs.filter(almacen__sucursal_id=sucursal_id)
        if almacen_id: qs = qs.filter(almacen_id=almacen_id)
        if producto_variante_id: qs = qs.filter(producto_variante_id=producto_variante_id)
        if almacen_id: qs = qs.filter(almacen_id=almacen_id)
        if producto_id:
            qs = qs.filter(
                models.Q(producto_id=producto_id) |
                models.Q(producto_variante__producto_id=producto_id)
            )
        if color_id: qs = qs.filter(producto_variante__color_id=color_id)
        if talla_id: qs = qs.filter(producto_variante__talla_id=talla_id)
        if sku_q: qs = qs.filter(producto_variante__sku__icontains=sku_q)

        limit_raw = (qp.get("limit") or "").strip()
        limit = None
        if limit_raw.lower() in {"all", "0", "-1"}:
            limit = None
        else:
            try:
                limit = int(limit_raw) if limit_raw else 200
            except Exception:
                limit = 200

        # Listado más reciente primero por la fecha real de la existencia;
        # ``-id`` como desempate estable. El recorte por ``limit`` va después,
        # así que se queda con las existencias movidas más recientemente.
        # ``nulls_last``: ``fecha_actualizacion`` se agregó como NULL-able
        # (migración 0011) y las filas previas siguen en NULL; en PostgreSQL un
        # DESC pone los NULL primero y se llevarían el ``limit`` entero.
        self._limite_listado = None if limit is None else max(1, min(limit, 2000))
        return qs.order_by(models.F("fecha_actualizacion").desc(nulls_last=True), "-id")

    def filter_queryset(self, queryset):
        # El recorte por ``limit`` es solo del listado y va al final, después de
        # ``?ordering=``. En ``get_queryset`` rompía ``get_object`` (detalle
        # propio = 404) y el ``OrderingFilter`` (500) sobre el slice (#835).
        queryset = super().filter_queryset(queryset)
        limite = getattr(self, "_limite_listado", None)
        if self.action == "list" and limite is not None:
            return queryset[:limite]
        return queryset

    def _validar_acceso_almacen(self, almacen):
        # Antes del save(): el almacén destino debe estar en el alcance del
        # usuario (el mismo de ``get_queryset``). Superusuario sin restricción.
        user = self.request.user
        if user.is_superuser or almacen is None:
            return
        if not self._almacenes_en_alcance().filter(pk=almacen.pk).exists():
            if not user.sucursales.filter(pk=almacen.sucursal_id).exists():
                raise PermissionDenied("No tiene acceso a la sucursal de este almacén")
            raise PermissionDenied("No tiene acceso a la empresa de este almacén")

    def perform_create(self, serializer):
        self._validar_acceso_almacen(serializer.validated_data.get("almacen"))
        serializer.save()

    def perform_update(self, serializer):
        self._validar_acceso_almacen(
            serializer.validated_data.get("almacen", serializer.instance.almacen)
        )
        serializer.save()

    # --- Pantalla de stock por almacén (EC-774) ---------------------------------
    # Acciones propias, de solo lectura: el listado de arriba, su ``limit`` y su
    # serializer no cambian. Detalle en ``existencias_por_almacen.py``.

    def _por_almacen_base(self, request):
        almacen_id = por_almacen_mod.almacen_id_requerido(request)
        qs = Existencia.objects.filter(almacen_id=almacen_id)
        # Mismo alcance que ``get_queryset``: un almacén ajeno da vacío, no 403.
        if not request.user.is_superuser:
            qs = qs.filter(almacen__in=self._almacenes_en_alcance())
        maximo = por_almacen_mod.max_stock(qs)
        return almacen_id, maximo, por_almacen_mod.anotar(qs, maximo)

    @extend_schema(
        summary="Existencias de un almacén, paginadas (pantalla de stock)",
        parameters=[
            OpenApiParameter("almacen_id", OpenApiTypes.INT, required=True, description="Almacén (obligatorio)."),
            OpenApiParameter("search", OpenApiTypes.STR, description="Nombre o SKU (producto, variante o muestra), contiene."),
            OpenApiParameter("nombre", OpenApiTypes.STR, description="Nombre exacto (opciones en resumen)."),
            OpenApiParameter("tipo", OpenApiTypes.STR, description="Código de tipo exacto."),
            OpenApiParameter("color", OpenApiTypes.STR, description="Nombre de color exacto."),
            OpenApiParameter("ubicacion", OpenApiTypes.INT, description="id_ubicacion."),
            OpenApiParameter("existencias", OpenApiTypes.STR, enum=list(por_almacen_mod.RANGOS_EXISTENCIAS)),
            OpenApiParameter("mes", OpenApiTypes.STR, description="AAAA-MM de fecha_actualizacion (hora de México)."),
            OpenApiParameter("estado", OpenApiTypes.STR, enum=list(por_almacen_mod.ESTADOS)),
            OpenApiParameter(
                "ordering", OpenApiTypes.STR,
                description="Separado por comas, '-' = descendente: " + ", ".join(por_almacen_mod.CAMPOS_ORDENAMIENTO),
            ),
        ],
        # Con ``pagination_class`` en la acción, drf-spectacular publica el sobre
        # ``{count, next, previous, results}`` y ``limit``/``offset``, como en los
        # listados de OC y proveedores. Sin ``?limit=`` la respuesta es el arreglo.
        responses=ExistenciaPorAlmacenSerializer(many=True),
    )
    @action(
        detail=False, methods=["get"], url_path="por-almacen",
        pagination_class=PaginacionOpcionalLimitOffset,
    )
    def por_almacen(self, request):
        _, maximo, qs = self._por_almacen_base(request)
        qs = por_almacen_mod.filtrar(request, qs, maximo)
        qs = por_almacen_mod.OrdenamientoPorAlmacenFilter().filter_queryset(request, qs, self)
        filas = por_almacen_mod.filas(qs)
        page = self.paginate_queryset(filas)
        if page is None:
            return Response(ExistenciaPorAlmacenSerializer(filas, many=True).data)
        return self.get_paginated_response(ExistenciaPorAlmacenSerializer(page, many=True).data)

    @extend_schema(
        summary="KPIs, max_stock y opciones de filtros de un almacén (pantalla de stock)",
        parameters=[
            OpenApiParameter("almacen_id", OpenApiTypes.INT, required=True, description="Almacén (obligatorio)."),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"], url_path="por-almacen/resumen")
    def por_almacen_resumen(self, request):
        almacen_id, maximo, qs = self._por_almacen_base(request)
        return Response({"almacen_id": almacen_id, **por_almacen_mod.resumen(qs, maximo)})

    def _report_to_int(self, value):
        return reportes_mod.to_int(value)

    def _report_to_decimal(self, value):
        return reportes_mod.to_decimal(value)

    def _quantize_qty(self, value):
        return reportes_mod.quantize_qty(value)

    def _build_report_almacenes_queryset(self):
        return reportes_mod.almacenes_del_reporte(self.request.user, self.request.query_params)

    @action(
        detail=False, methods=["get"], url_path="reporte-existencias-periodo",
        permission_classes=PERMISOS_REPORTES,
    )
    def reporte_existencias_periodo(self, request):
        # Cálculo completo (alcance + filtros + totales) en ``reportes_periodo``;
        # aquí solo se pagina y se arma el sobre JSON.
        reporte = reportes_mod.reporte_existencias(request.user, request.query_params)
        if reporte["sin_almacenes"]:
            return Response(
                {
                    "fecha_inicio": str(reporte["fecha_inicio"]),
                    "fecha_final": str(reporte["fecha_final"]),
                    "filtros": {
                        "producto_id": reporte["producto_id"],
                        "producto_variante_id": reporte["producto_variante_id"],
                    },
                    "resumen": reporte["resumen"],
                    "resumen_por_almacen": [],
                    "count": 0,
                    "next": None,
                    "previous": None,
                    "results": [],
                },
                status=status.HTTP_200_OK,
            )

        results = reporte["results"]
        paginator = ReporteExistenciasPeriodoPagination()
        page = paginator.paginate_queryset(results, request, view=self)

        response = paginator.get_paginated_response(page)
        response.data["fecha_inicio"] = str(reporte["fecha_inicio"])
        response.data["fecha_final"] = str(reporte["fecha_final"])
        # El almacén pedido (``null`` sin filtro). Antes del EC-814 salía el
        # almacén de la última fila: una variable del cálculo lo pisaba.
        response.data["filtros"] = {"almacen_id": reporte["almacen_id"]}
        response.data["resumen"] = reporte["resumen"]
        response.data["resumen_por_almacen"] = reporte["resumen_por_almacen"]
        return response

    @extend_schema(
        summary="Reporte de existencias por periodo en PDF o XLSX",
        description=(
            "Mismos parámetros, alcance y validación que reporte-existencias-periodo/, "
            "con el reporte completo (sin paginar). A diferencia del JSON, el almacén es "
            "obligatorio: todos los almacenes juntos tardan más de lo aceptable."
        ),
        parameters=[
            PARAMETRO_FORMATO_EXPORTACION,
            *PARAMETROS_FECHAS_REPORTE,
            OpenApiParameter("almacen", OpenApiTypes.INT, required=True, description="Almacén (alias almacen_id)."),
            OpenApiParameter("empresa", OpenApiTypes.INT, description="Empresa (alias empresa_id)."),
            OpenApiParameter("sucursal", OpenApiTypes.INT, description="Sucursal (alias sucursal_id)."),
            OpenApiParameter("producto", OpenApiTypes.INT, description="Producto (alias producto_id)."),
            OpenApiParameter("producto_variante", OpenApiTypes.INT, description="Variante (alias producto_variante_id)."),
        ],
        responses=RESPUESTAS_EXPORTACION,
    )
    @action(
        detail=False, methods=["get"], url_path="reporte-existencias-periodo/exportar",
        renderer_classes=RENDERERS_EXPORTACION, permission_classes=PERMISOS_REPORTES,
    )
    def reporte_existencias_periodo_exportar(self, request):
        formato = exportacion_mod.formato_requerido(request.query_params)
        # Fechas primero: sin ellas, el mismo 400 que el JSON.
        reportes_mod.parse_fechas(request.query_params)
        reportes_mod.almacen_requerido(request.query_params)
        reporte = reportes_mod.reporte_existencias(request.user, request.query_params)
        return respuesta_archivo(exportacion_mod.exportar_existencias(reporte, formato))

    @action(detail=False, methods=["get"], url_path="reporte-existencias-produccion-compras")
    def reporte_existencias_produccion_compras(self, request):
        """Disponible (almacenes Producto Terminado) + lo que viene en camino
        (OP activas y OC pendientes), agrupado por producto con desglose por
        talla en disponible/producción — para que mesa de control vea cuándo
        van a llegar las órdenes de producción y las compras pendientes de un
        SKU, sin cruzar pantallas de producción y compras.

        Compras: ``compras_pendiente_cantidad`` es el total del producto;
        ``compras_por_talla`` desglosa solo los renglones de OC con
        ``producto_variante`` (O de PT, #763). Los de MP o legados sin variante
        quedan solo en el total.

        Producción y compras cuentan solo lo **pendiente** de cada renglón: lo
        pedido menos lo recibido en recepciones activas no canceladas, menos lo
        que Calidad rechaza — la misma regla que ``recepciones/onboarding`` (EC-785).
        Se calcula en SQL (subconsultas por FK indexada) y solo bajan los
        renglones con pendiente (EC-777). ``?sucursal``/``?almacen`` acotan
        también OP y OC a la sucursal de esos almacenes. ``?limit=&offset=``
        pagina los productos (sin ``limit``, la lista completa como antes).
        """
        from produccion.models import OrdenProduccion, OrdenProduccionDetalle
        from compras.models import (
            CalidadInspeccionDetalle, OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle,
        )
        from nucleo.listados import PaginacionOpcionalLimitOffset

        decimal_qty = models.DecimalField(max_digits=18, decimal_places=4)

        def _pendiente(campo_detalle):
            """``cantidad`` − recibido neto del renglón (``campo_detalle`` es el FK de
            ``RecepcionDetalle`` al renglón de origen), como expresión SQL."""
            def _suma(qs, valor):
                return Coalesce(
                    Subquery(
                        qs.order_by().values(campo_detalle).annotate(t=models.Sum(valor)).values("t")[:1],
                        output_field=decimal_qty,
                    ),
                    Value(Decimal("0")),
                    output_field=decimal_qty,
                )
            recibido = (
                RecepcionDetalle.objects.filter(**{campo_detalle: OuterRef("pk")}, recepcion__activo=True)
                .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            )
            rechazado = (
                CalidadInspeccionDetalle.objects.filter(
                    **{f"recepcion_detalle__{campo_detalle}": OuterRef("pk")},
                    recepcion_detalle__recepcion__activo=True,
                )
                .exclude(recepcion_detalle__recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            )
            # ``values(campo)`` agrupa por el renglón; en ``rechazado`` el campo va con el prefijo.
            rechazado_sum = Coalesce(
                Subquery(
                    rechazado.order_by().values(f"recepcion_detalle__{campo_detalle}")
                    .annotate(t=models.Sum("cantidad_rechazada")).values("t")[:1],
                    output_field=decimal_qty,
                ),
                Value(Decimal("0")),
                output_field=decimal_qty,
            )
            return ExpressionWrapper(
                F("cantidad") - (_suma(recibido, "cantidad_recibida") - rechazado_sum),
                output_field=decimal_qty,
            )

        user = request.user
        qp = request.query_params
        producto_id = self._report_to_int(qp.get("producto") or qp.get("producto_id"))
        sku_q = (qp.get("sku") or qp.get("q") or "").strip()

        almacenes_pt = self._build_report_almacenes_queryset().filter(
            tipo_almacen=TipoAlmacen.PRODUCTO_TERMINADO
        )
        almacenes_info = list(almacenes_pt.values("id_almacen", "empresa_id", "sucursal_id"))
        almacen_ids = {a["id_almacen"] for a in almacenes_info}
        if not almacen_ids:
            return Response({"resultados": []})
        empresa_ids = {a["empresa_id"] for a in almacenes_info}
        # Con ``?sucursal``/``?almacen`` el reporte es de esa sucursal también en OP y OC.
        acotar_sucursal = bool(
            self._report_to_int(qp.get("sucursal") or qp.get("sucursal_id"))
            or self._report_to_int(qp.get("almacen") or qp.get("almacen_id"))
        )
        sucursal_ids = {a["sucursal_id"] for a in almacenes_info}

        # --- 1) Disponible: Existencia en almacenes PT, por producto+talla ---
        existencias = (
            Existencia.objects.filter(almacen_id__in=almacen_ids)
            .select_related("producto", "producto_variante__producto", "producto_variante__talla")
        )
        if producto_id:
            existencias = existencias.filter(
                models.Q(producto_id=producto_id) | models.Q(producto_variante__producto_id=producto_id)
            )
        if sku_q:
            existencias = existencias.filter(
                models.Q(producto_variante__sku__icontains=sku_q)
                | models.Q(producto__nombre__icontains=sku_q)
                | models.Q(producto__codigo__icontains=sku_q)
            )

        productos = {}

        def _producto_bucket(producto):
            if producto.pk not in productos:
                productos[producto.pk] = {
                    "producto_id": producto.pk,
                    "codigo": producto.codigo,
                    "descripcion": producto.nombre,
                    "disponible_por_talla": defaultdict(lambda: Decimal("0")),
                    "produccion_por_talla": defaultdict(lambda: Decimal("0")),
                    "ordenes_produccion": {},
                    "compras_pendiente_cantidad": Decimal("0"),
                    "compras_por_talla": defaultdict(lambda: Decimal("0")),
                    "ordenes_compra": {},
                }
            return productos[producto.pk]

        for ex in existencias.iterator():
            producto = ex.producto or getattr(ex.producto_variante, "producto", None)
            if producto is None:
                continue
            talla = getattr(ex.producto_variante, "talla", None)
            talla_nombre = talla.nombre if talla else "N/A"
            _producto_bucket(producto)["disponible_por_talla"][talla_nombre] += self._report_to_decimal(ex.cantidad)

        # --- 2) En producción: OrdenProduccionDetalle de OP activas, no completadas/canceladas ---
        op_detalles = (
            OrdenProduccionDetalle.objects.filter(
                op__activo=True,
                op__empresa_id__in=empresa_ids,
            )
            .exclude(op__estatus_op__in=[
                OrdenProduccion.EstatusOrdenProduccion.COMPLETADO,
                OrdenProduccion.EstatusOrdenProduccion.CANCELADO,
            ])
            .annotate(pendiente=_pendiente("orden_produccion_detalle"))
            .filter(pendiente__gt=0)
            .select_related("producto_variante__producto", "producto_variante__talla", "op")
        )
        if acotar_sucursal:
            op_detalles = op_detalles.filter(op__sucursal_id__in=sucursal_ids)
        if producto_id:
            op_detalles = op_detalles.filter(producto_variante__producto_id=producto_id)
        if sku_q:
            op_detalles = op_detalles.filter(
                models.Q(producto_variante__sku__icontains=sku_q)
                | models.Q(producto_variante__producto__nombre__icontains=sku_q)
                | models.Q(producto_variante__producto__codigo__icontains=sku_q)
            )

        for det in op_detalles.iterator():
            variante = det.producto_variante
            if variante is None or variante.producto_id is None:
                continue
            bucket = _producto_bucket(variante.producto)
            talla_nombre = variante.talla.nombre if variante.talla else "N/A"
            bucket["produccion_por_talla"][talla_nombre] += self._report_to_decimal(det.pendiente)
            op = det.op
            bucket["ordenes_produccion"][op.pk] = {
                "folio": op.folio_op,
                "estatus_display": op.get_estatus_op_display(),
                "fecha_entrega_estimada": (
                    str(op.fecha_entrega_estimada) if op.fecha_entrega_estimada else None
                ),
                "comentarios": op.observaciones,
            }

        # --- 3) Por llegar de compras: OC no recibidas del todo, por producto (y talla si hay variante) ---
        oc_detalles = (
            OrdenCompraDetalle.objects.filter(
                orden_compra__empresa_id__in=empresa_ids,
                orden_compra__activo=True,
            )
            .exclude(orden_compra__estatus__in=[
                OrdenCompra.EstatusOrdenCompra.RECIBIDA,
                OrdenCompra.EstatusOrdenCompra.CANCELADA,
                OrdenCompra.EstatusOrdenCompra.BORRADOR,
            ])
            .filter(producto__isnull=False)
            .annotate(pendiente=_pendiente("orden_compra_detalle"))
            .filter(pendiente__gt=0)
            .select_related("producto", "producto_variante__talla", "orden_compra")
        )
        if acotar_sucursal:
            oc_detalles = oc_detalles.filter(orden_compra__sucursal_id__in=sucursal_ids)
        if producto_id:
            oc_detalles = oc_detalles.filter(
                models.Q(producto_id=producto_id) | models.Q(producto_variante__producto_id=producto_id)
            )
        if sku_q:
            oc_detalles = oc_detalles.filter(
                models.Q(producto_variante__sku__icontains=sku_q)
                | models.Q(producto__nombre__icontains=sku_q)
                | models.Q(producto__codigo__icontains=sku_q)
            )

        for det in oc_detalles.iterator():
            pendiente = self._report_to_decimal(det.pendiente)
            bucket = _producto_bucket(det.producto)
            bucket["compras_pendiente_cantidad"] += pendiente
            if det.producto_variante_id:
                talla = det.producto_variante.talla
                bucket["compras_por_talla"][talla.nombre if talla else "N/A"] += pendiente
            oc = det.orden_compra
            bucket["ordenes_compra"][oc.pk] = {
                "folio": oc.folio,
                "estatus_display": oc.get_estatus_display(),
                "fecha_entrega_estimada": (
                    str(oc.fecha_entrega_estimada) if oc.fecha_entrega_estimada else None
                ),
                "comentarios": oc.observaciones,
            }

        resultados = []
        for bucket in productos.values():
            disponible = bucket["disponible_por_talla"]
            produccion = bucket["produccion_por_talla"]
            tallas = sorted(set(disponible) | set(produccion))
            resultados.append({
                "producto_id": bucket["producto_id"],
                "codigo": bucket["codigo"],
                "descripcion": bucket["descripcion"],
                "disponible_por_talla": {
                    t: str(self._quantize_qty(disponible.get(t, Decimal("0")))) for t in tallas
                },
                "produccion_por_talla": {
                    t: str(self._quantize_qty(produccion.get(t, Decimal("0")))) for t in tallas
                },
                "total_por_talla": {
                    t: str(self._quantize_qty(disponible.get(t, Decimal("0")) + produccion.get(t, Decimal("0"))))
                    for t in tallas
                },
                "disponible_total": str(self._quantize_qty(sum(disponible.values(), Decimal("0")))),
                "produccion_total": str(self._quantize_qty(sum(produccion.values(), Decimal("0")))),
                "compras_pendiente_cantidad": str(self._quantize_qty(bucket["compras_pendiente_cantidad"])),
                "compras_por_talla": {
                    t: str(self._quantize_qty(c)) for t, c in sorted(bucket["compras_por_talla"].items())
                },
                "ordenes_produccion": list(bucket["ordenes_produccion"].values()),
                "ordenes_compra": list(bucket["ordenes_compra"].values()),
            })

        resultados.sort(key=lambda r: r["codigo"] or "")
        # ``?limit=`` pagina los productos (EC-777); sin él, la respuesta de siempre.
        paginador = PaginacionOpcionalLimitOffset()
        pagina = paginador.paginate_queryset(resultados, request, view=self)
        if pagina is None:
            return Response({"resultados": resultados})
        respuesta = paginador.get_paginated_response(pagina)
        respuesta.data["resultados"] = respuesta.data.pop("results")
        return respuesta


class OperacionInventarioViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticatedAndScoped]

    def _to_int(self, v):
        if v in (None, ""):
            return None
        try:
            return int(v)
        except Exception:
            return None

    def _to_decimal(self, v):
        if v in (None, ""):
            return None
        try:
            return Decimal(str(v))
        except Exception:
            return None

    def _get_almacen(self, request):
        almacen_id = self._to_int(request.data.get("almacen") or request.data.get("almacen_id"))
        if not almacen_id:
            raise ValidationError({"almacen": "Almacén es requerido."})
        qs = Almacen.objects.select_related("empresa", "sucursal")
        # Aislamiento multi-tenant: mismo alcance que existencias. Un almacén
        # fuera de alcance se trata como no encontrado (igual que un pedido de
        # otra empresa en ``_get_pedido``), sin revelar que existe.
        if not request.user.is_superuser:
            qs = qs.filter(pk__in=almacenes_en_alcance(request.user))
        almacen = qs.filter(pk=almacen_id).first()
        if not almacen:
            raise ValidationError({"almacen": "Almacén no encontrado."})
        return almacen

    def _get_pedido(self, request, almacen):
        """
        Resuelve un pedido OPCIONAL del body (acepta `pedido` o `pedido_id`,
        replicando el patrón de `almacen`/`almacen_id`).

        - Omitido/None/"" -> None (comportamiento actual: sin vínculo).
        - Presente pero inválido -> 400 (no se ignora silenciosamente).
        - Presente y válido pero de otra empresa (o inexistente) -> 400
          (aislamiento multi-tenant: un pedido de otra empresa se trata como
          no encontrado, nunca se vincula).
        """
        raw = request.data.get("pedido")
        if raw is None:
            raw = request.data.get("pedido_id")
        if raw in (None, ""):
            return None
        pedido_id = self._to_int(raw)
        if not pedido_id:
            raise ValidationError({"pedido": "pedido inválido."})
        # Validar contra la MISMA empresa que usará el movimiento formal
        # (idéntica resolución que _crear_movimiento_formal).
        empresa, _sucursal = self._resolve_empresa_sucursal(request, almacen)
        pedido = Pedido.objects.filter(pk=pedido_id, empresa=empresa).first()
        if not pedido:
            raise ValidationError({"pedido": "Pedido no encontrado."})
        return pedido

    def _get_items(self, request, almacen):
        items = request.data.get("items") or request.data.get("detalle") or []
        if not isinstance(items, list) or not items:
            raise ValidationError({"items": "items debe ser una lista no vacía."})
        normalized = []
        producto_ids = set()
        producto_variante_ids = set()
        muestra_ids, muestra_skus = set(), set()
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                raise ValidationError({"items": f"Item #{idx+1} inválido."})
            producto_id = self._to_int(it.get("producto") or it.get("producto_id"))
            pv_id = self._to_int(it.get("producto_variante") or it.get("producto_variante_id"))
            # SKU de muestra (``VarianteProductoProduccion``): por id o por SKU.
            vp_id = self._to_int(it.get("variante_produccion") or it.get("variante_produccion_id"))
            sku_muestra = (it.get("sku_muestra") or "").strip() or None
            es_muestra = bool(vp_id or sku_muestra)
            if es_muestra and (producto_id or pv_id):
                raise ValidationError(
                    {"items": f"Item #{idx+1}: un SKU de muestra no lleva producto ni producto_variante."}
                )
            if not producto_id and not pv_id and not es_muestra:
                raise ValidationError(
                    {"items": f"Item #{idx+1}: producto, producto_variante o variante_produccion es requerido."}
                )
            qty = self._to_decimal(it.get("cantidad"))
            if qty is None:
                raise ValidationError({"items": f"Item #{idx+1}: cantidad inválida."})
            ubicacion_id = self._to_int(it.get("ubicacion") or it.get("ubicacion_id"))
            lote_id = self._to_int(it.get("lote") or it.get("lote_id"))
            serie_id = self._to_int(it.get("serie") or it.get("serie_id"))
            if producto_id:
                producto_ids.add(producto_id)
            if pv_id:
                producto_variante_ids.add(pv_id)
            if vp_id:
                muestra_ids.add(vp_id)
            elif sku_muestra:
                muestra_skus.add(sku_muestra)
            normalized.append(
                {
                    "producto_id": producto_id,
                    "producto_variante_id": pv_id,
                    "variante_produccion_id": vp_id,
                    "sku_muestra": sku_muestra,
                    "cantidad": qty,
                    "ubicacion_id": ubicacion_id,
                    "lote_id": lote_id,
                    "serie_id": serie_id,
                }
            )

        variantes = {
            variante.pk: variante
            for variante in ProductoVariante.objects.filter(pk__in=producto_variante_ids).only(
                "id", "producto_id", "empresa_id"
            )
        }
        producto_ids.update(
            variante.producto_id for variante in variantes.values() if getattr(variante, "producto_id", None)
        )
        productos = {
            producto.pk: producto
            for producto in Producto.objects.filter(pk__in=producto_ids).only("id", "empresa_id")
        }
        # SKUs de muestra de la empresa del almacén (aislamiento para todos).
        muestras_por_id, muestras_por_sku = {}, {}
        if muestra_ids or muestra_skus:
            for muestra in VarianteProductoProduccion.objects.filter(
                models.Q(pk__in=muestra_ids) | models.Q(sku__in=muestra_skus),
                empresa_id=almacen.empresa_id, activo=True,
            ).only("id", "sku"):
                muestras_por_id[muestra.pk] = muestra
                muestras_por_sku[muestra.sku] = muestra
        # La ubicación debe ser del almacén de la operación (regla que ya existía;
        # implica la misma empresa). Se resuelve aquí, antes de cualquier escritura.
        ubicaciones = {
            ubicacion.pk: ubicacion
            for ubicacion in Ubicacion.objects.filter(
                pk__in={it["ubicacion_id"] for it in normalized if it["ubicacion_id"]},
                almacen_id=almacen.pk,
            )
        }

        for idx, item in enumerate(normalized):
            variante_id = item["producto_variante_id"]
            producto_id = item["producto_id"]

            if item["variante_produccion_id"] or item["sku_muestra"]:
                muestra = (
                    muestras_por_id.get(item["variante_produccion_id"]) if item["variante_produccion_id"]
                    else muestras_por_sku.get(item["sku_muestra"])
                )
                if not muestra:
                    raise ValidationError({"items": f"Item #{idx+1}: SKU de muestra no encontrado."})
                item["variante_produccion_id"] = muestra.pk
                item["ubicacion"] = None
                if item["ubicacion_id"]:
                    item["ubicacion"] = ubicaciones.get(item["ubicacion_id"])
                    if not item["ubicacion"]:
                        raise ValidationError({"ubicacion": "Ubicación inválida para el almacén."})
                continue

            if variante_id:
                variante = variantes.get(variante_id)
                if not variante:
                    raise ValidationError(
                        {"items": f"Item #{idx+1}: producto_variante no encontrado."}
                    )
                if producto_id and variante.producto_id != producto_id:
                    raise ValidationError(
                        {
                            "items": (
                                f"Item #{idx+1}: producto y producto_variante no coinciden."
                            )
                        }
                    )
                # Aislamiento multi-tenant, para TODOS (superusuario incluido).
                if variante.empresa_id != almacen.empresa_id:
                    raise ValidationError(
                        {"items": f"Item #{idx+1}: producto_variante no pertenece a la empresa del almacén."}
                    )
                item["producto_id"] = variante.producto_id

            if not item["producto_id"] or item["producto_id"] not in productos:
                raise ValidationError({"items": f"Item #{idx+1}: producto no encontrado."})
            if productos[item["producto_id"]].empresa_id != almacen.empresa_id:
                raise ValidationError(
                    {"items": f"Item #{idx+1}: producto no pertenece a la empresa del almacén."}
                )

            item["ubicacion"] = None
            if item["ubicacion_id"]:
                item["ubicacion"] = ubicaciones.get(item["ubicacion_id"])
                if not item["ubicacion"]:
                    raise ValidationError({"ubicacion": "Ubicación inválida para el almacén."})
        return normalized

    def _validar_empresa_sucursal_del_body(self, request, almacen):
        # ``empresa``/``sucursal`` salen SIEMPRE del almacén. Si el body manda
        # alguna, debe coincidir: una que la contradiga se rechaza en vez de
        # ignorarse en silencio.
        for campo, actual in (("empresa", almacen.empresa_id), ("sucursal", almacen.sucursal_id)):
            raw = request.data.get(campo) or request.data.get(f"{campo}_id")
            if raw in (None, ""):
                continue
            if self._to_int(raw) != actual:
                raise ValidationError({campo: f"No corresponde a la {campo} del almacén."})

    def _resolve_empresa_sucursal(self, request, almacen):
        # Derivadas del almacén (ya validado contra el alcance del usuario); ni
        # el usuario ni el body son fuente del tenant.
        return almacen.empresa, almacen.sucursal

    def _crear_ajuste(self, request, almacen):
        empresa_obj, sucursal_obj = self._resolve_empresa_sucursal(request, almacen)
        if not (empresa_obj and sucursal_obj):
            return None
        motivo = (request.data.get("motivo") or "Ajuste").strip()[:100]
        observaciones = (request.data.get("observaciones") or "").strip()[:150] or None
        ajuste = AjusteInventario.objects.create(
            empresa=empresa_obj,
            sucursal=sucursal_obj,
            almacen=almacen,
            usuario=request.user,
            motivo=motivo,
            observaciones=observaciones,
        )
        return ajuste.pk

    def _crear_movimiento_formal(self, request, tipo, almacen, ajuste_id, detalle_movimientos, pedido=None):
        empresa, sucursal = self._resolve_empresa_sucursal(request, almacen)
        if not empresa or not sucursal:
            return None

        movimiento = MovimientoInventario.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            pedido=pedido,
            entrega_id=None,
            devolucion_id=None,
            ajuste_inventario_id=ajuste_id,
            tipo_movimiento=tipo,
            usuario=request.user,
            observaciones=(request.data.get("observaciones") or request.data.get("motivo") or None),
            recepcion_id=None,
            transferencia_id=None,
            op_id=None,
        )

        renglones = []
        for item in detalle_movimientos:
            detalle = MovimientoInventarioDetalle.objects.create(
                movimiento_inventario=movimiento,
                producto_id=item["producto_id"],
                producto_variante_id=item["producto_variante_id"],
                variante_produccion_id=item["variante_produccion_id"],
                ubicacion_origen_id=item["ubicacion_origen_id"],
                ubicacion_destino_id=item["ubicacion_destino_id"],
                almacen_id=item["almacen_id"],
                lote_id=item["lote_id"],
                serie_id=item["serie_id"],
                cantidad=item["cantidad"],
                costo_unitario=Decimal("0"),
            )
            # En un ajuste ``cantidad`` es la final: el delta viaja aparte.
            renglones.append((detalle, item["delta"], None))
        # Entradas, salidas y ajustes manuales al costo promedio vigente (#809).
        costeo.costear(empresa.pk, renglones, costeo.Origen.OPERACION)
        return movimiento

    def _apply(self, request, tipo):
        tipo = (tipo or "").strip().upper()
        if tipo not in {"ENTRADA", "SALIDA", "AJUSTE"}:
            raise ValidationError({"tipo": "Tipo inválido."})

        almacen = self._get_almacen(request)
        self._validar_empresa_sucursal_del_body(request, almacen)
        items = self._get_items(request, almacen)
        # Pedido OPCIONAL: validado (y aislado por empresa) antes de tocar la BD.
        pedido = self._get_pedido(request, almacen)

        if tipo in {"ENTRADA", "SALIDA"}:
            for it in items:
                if it["cantidad"] <= 0:
                    raise ValidationError({"items": "cantidad debe ser > 0 para entradas/salidas."})
        elif tipo == "AJUSTE":
            for it in items:
                if it["cantidad"] < 0:
                    raise ValidationError({"items": "En ajuste, cantidad debe ser >= 0 (cantidad final)."})

        user = request.user
        results = []
        before_after = []
        detalle_movimientos = []
        # Toda la validación de alcance y pertenencia ya corrió arriba. Desde la
        # primera escritura (el AjusteInventario incluido) todo va en un solo
        # atomic: si algo falla, no queda nada a medias.
        with transaction.atomic():
            ajuste_id = self._crear_ajuste(request, almacen) if tipo == "AJUSTE" else None
            for it in items:
                # Ya validada en ``_get_items`` (antes de cualquier escritura).
                ubicacion = it["ubicacion"]

                ex = (
                    Existencia.objects.select_for_update()
                    .filter(almacen_id=almacen.pk, ubicacion_id=(ubicacion.pk if ubicacion else None))
                    .order_by("id")
                )
                if it["variante_produccion_id"]:
                    ex = ex.filter(variante_produccion_id=it["variante_produccion_id"])
                elif it["producto_variante_id"]:
                    ex = ex.filter(producto_variante_id=it["producto_variante_id"])
                else:
                    ex = ex.filter(
                        producto_id=it["producto_id"],
                        producto_variante__isnull=True,
                        variante_produccion__isnull=True,
                    )
                ex = ex.first()
                if not ex and tipo == "SALIDA":
                    fallback = (
                        Existencia.objects.select_for_update()
                        .filter(almacen_id=almacen.pk)
                        .order_by("-cantidad", "id")
                    )
                    if it["variante_produccion_id"]:
                        fallback = fallback.filter(variante_produccion_id=it["variante_produccion_id"])
                    elif it["producto_variante_id"]:
                        fallback = fallback.filter(producto_variante_id=it["producto_variante_id"])
                    else:
                        fallback = fallback.filter(
                            producto_id=it["producto_id"],
                            producto_variante__isnull=True,
                            variante_produccion__isnull=True,
                        )
                    fallback = fallback.first()
                    ex = fallback

                if not ex:
                    if tipo == "SALIDA":
                        raise ValidationError(
                            {"cantidad": "No hay existencia para realizar la salida."}
                        )
                    ex = Existencia.objects.create(
                        producto_id=it["producto_id"],
                        producto_variante_id=it["producto_variante_id"],
                        variante_produccion_id=it["variante_produccion_id"],
                        almacen=almacen,
                        ubicacion=ubicacion,
                        stock=0,
                        cantidad=Decimal("0"),
                    )

                current = ex.cantidad or Decimal("0")
                qty = it["cantidad"]
                ubicacion_origen_id = ex.ubicacion_id if tipo in {"SALIDA", "AJUSTE"} else None
                ubicacion_destino_id = ex.ubicacion_id if tipo == "ENTRADA" else None

                if tipo == "ENTRADA":
                    new_qty = current + qty
                elif tipo == "SALIDA":
                    new_qty = current - qty
                else:
                    new_qty = qty
                    ubicacion_destino_id = ex.ubicacion_id

                if new_qty < 0:
                    raise ValidationError({"cantidad": "La operación deja stock negativo."})

                ex.cantidad = new_qty
                try:
                    ex.stock = int(new_qty)
                except Exception:
                    ex.stock = ex.stock or 0
                ex.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])

                before_after.append(
                    {
                        "producto_id": ex.producto_id,
                        "producto_variante_id": ex.producto_variante_id,
                        "variante_produccion_id": ex.variante_produccion_id,
                        "ubicacion_id": ex.ubicacion_id,
                        "cantidad_before": str(current),
                        "cantidad_after": str(new_qty),
                        "delta": str(new_qty - current),
                    }
                )
                detalle_movimientos.append(
                    {
                        "producto_id": ex.producto_id,
                        # ``ex`` se buscó por la variante de la línea (o sin
                        # variante si la línea no trae): es la misma.
                        "producto_variante_id": ex.producto_variante_id,
                        "variante_produccion_id": ex.variante_produccion_id,
                        "ubicacion_origen_id": ubicacion_origen_id,
                        "ubicacion_destino_id": ubicacion_destino_id,
                        # ENTRADA/SALIDA: el almacén de la existencia, como en
                        # ventas (EC-821). El ajuste lo da su cabecera.
                        "almacen_id": ex.almacen_id if tipo in {"ENTRADA", "SALIDA"} else None,
                        "lote_id": it["lote_id"],
                        "serie_id": it["serie_id"],
                        "cantidad": qty if tipo != "AJUSTE" else new_qty,
                        "delta": new_qty - current,
                    }
                )
                results.append(
                    {
                        "id": ex.pk,
                        "producto_id": ex.producto_id,
                        "producto_variante_id": ex.producto_variante_id,
                        "variante_produccion_id": ex.variante_produccion_id,
                        "almacen_id": ex.almacen_id,
                        "ubicacion_id": ex.ubicacion_id,
                        "cantidad": str(ex.cantidad),
                        "stock": ex.stock,
                    }
                )

            empresa_evt, _sucursal = self._resolve_empresa_sucursal(request, almacen)
            if empresa_evt:
                ip = ip_auditoria(request)
                ua = request.META.get("HTTP_USER_AGENT")
                ev = AuditoriaEvento.objects.create(
                    empresa=empresa_evt,
                    usuario=user if getattr(user, "pk", None) else None,
                    modulo="inventarios",
                    accion=tipo,
                    tabla="existencias",
                    id_registro=str(almacen.pk),
                    antes_json={"items": before_after, "ajuste_id": ajuste_id},
                    despues_json={
                        "almacen_id": almacen.pk,
                        "sucursal_id": almacen.sucursal_id,
                        "empresa_id": almacen.empresa_id,
                        "items": before_after,
                        "ajuste_id": ajuste_id,
                    },
                    ip=ip,
                    user_agent=ua,
                )
            else:
                ev = None

            movimiento_formal = self._crear_movimiento_formal(
                request=request,
                tipo=tipo,
                almacen=almacen,
                ajuste_id=ajuste_id,
                detalle_movimientos=detalle_movimientos,
                pedido=pedido,
            )

        return Response(
            {
                "tipo": tipo,
                "almacen_id": almacen.pk,
                "ajuste_id": ajuste_id,
                "movimiento_id": (getattr(ev, "id_evento", None) if ev else None),
                "movimiento_inventario_id": (getattr(movimiento_formal, "pk", None) if movimiento_formal else None),
                "result": results,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="entrada")
    def entrada(self, request):
        return self._apply(request, "ENTRADA")

    @action(detail=False, methods=["post"], url_path="salida")
    def salida(self, request):
        return self._apply(request, "SALIDA")

    @action(detail=False, methods=["post"], url_path="ajuste")
    def ajuste(self, request):
        return self._apply(request, "AJUSTE")


class SkuMuestraViewSet(viewsets.ViewSet):
    """Autocompletado de SKUs de muestra para la entrada a almacén.

    ``?search=`` busca en el SKU o en el folio del pedido; máximo 10. Una sola
    consulta con ``values()`` (sin instanciar modelos), acotada a la empresa del
    usuario; el superusuario sin empresa ve todas.
    """

    permission_classes = [IsAuthenticatedAndScoped]
    LIMITE = 10

    def list(self, request):
        texto = (request.query_params.get("search") or "").strip()
        if not texto:
            return Response([])
        user = request.user
        qs = VarianteProductoProduccion.objects.filter(activo=True, sku__isnull=False)
        empresa_id = getattr(user, "empresa_id", None)
        if empresa_id:
            qs = qs.filter(empresa_id=empresa_id)
        elif not user.is_superuser:
            return Response([])
        filas = (
            qs.filter(models.Q(sku__icontains=texto) | models.Q(pedido_detalle__pedido__folio__icontains=texto))
            .order_by("sku")
            .values("id", "sku", "nombre", "talla__nombre", "pedido_detalle__pedido__folio",
                    "pedido_detalle__producto_nombre_externo")[: self.LIMITE]
        )
        return Response([
            {
                "variante_produccion": f["id"],
                "sku_muestra": f["sku"],
                "pedido_folio": f["pedido_detalle__pedido__folio"],
                "nombre": f["nombre"] or f["pedido_detalle__producto_nombre_externo"],
                "talla": f["talla__nombre"],
            }
            for f in filas
        ])


class ReporteMovimientosPeriodoPagination(PageNumberPagination):
    # Instantiated explicitly inside reporte_movimientos_periodo, not set as
    # pagination_class on the ViewSet, so list()/detalles()/other actions are
    # unaffected. Mirrors ReporteExistenciasPeriodoPagination's final config
    # (page_size 200, client-overridable, no max cap).
    page_size = 200
    page_size_query_param = "page_size"


class MovimientoOperacionViewSet(ExportacionArchivoMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditoriaMovimientoSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def _base_queryset(self):
        return (
            AuditoriaEvento.objects.filter(modulo="inventarios", tabla="existencias")
            .select_related("empresa", "usuario")
            .order_by("-created_at", "-id_evento")
        )

    def _movement_report_allowed_almacen_ids(self, request):
        return reportes_mod.almacen_ids_movimientos(request.user, request.query_params)

    def _movement_report_parse_dates(self, request):
        return reportes_mod.parse_fechas(request.query_params)

    @action(
        detail=False, methods=["get"], url_path="reporte-movimientos-periodo",
        permission_classes=PERMISOS_REPORTES,
    )
    def reporte_movimientos_periodo(self, request):
        # Cálculo completo en ``reportes_periodo``; aquí solo se pagina.
        reporte = reportes_mod.reporte_movimientos(request.user, request.query_params)
        if reporte["sin_almacenes"]:
            return Response(
                {
                    "tipo_movimiento": reporte["tipo_movimiento"],
                    "fecha_inicio": str(reporte["fecha_inicio"]),
                    "fecha_final": str(reporte["fecha_final"]),
                    "filtros": {"almacen_id": reporte["almacen_id"]},
                    "resumen": reporte["resumen"],
                    "count": 0,
                    "next": None,
                    "previous": None,
                    "results": [],
                },
                status=status.HTTP_200_OK,
            )

        paginator = ReporteMovimientosPeriodoPagination()
        page = paginator.paginate_queryset(reporte["results"], request, view=self)

        response = paginator.get_paginated_response(page)
        response.data["tipo_movimiento"] = reporte["tipo_movimiento"]
        response.data["fecha_inicio"] = str(reporte["fecha_inicio"])
        response.data["fecha_final"] = str(reporte["fecha_final"])
        response.data["filtros"] = {"almacen_id": reporte["almacen_id"]}
        response.data["resumen"] = reporte["resumen"]
        return response

    @extend_schema(
        summary="Reporte de movimientos por periodo en PDF o XLSX",
        description=(
            "Mismos parámetros, alcance y validación que reporte-movimientos-periodo/, "
            "con el reporte completo (sin paginar)."
        ),
        parameters=[
            PARAMETRO_FORMATO_EXPORTACION,
            OpenApiParameter(
                "tipo_movimiento", OpenApiTypes.STR, required=True,
                enum=list(reportes_mod.TIPOS_MOVIMIENTO), description="Tipo de movimiento.",
            ),
            *PARAMETROS_FECHAS_REPORTE,
            OpenApiParameter("almacen", OpenApiTypes.INT, description="Almacén (alias almacen_id)."),
        ],
        responses=RESPUESTAS_EXPORTACION,
    )
    @action(
        detail=False, methods=["get"], url_path="reporte-movimientos-periodo/exportar",
        renderer_classes=RENDERERS_EXPORTACION, permission_classes=PERMISOS_REPORTES,
    )
    def reporte_movimientos_periodo_exportar(self, request):
        formato = exportacion_mod.formato_requerido(request.query_params)
        reporte = reportes_mod.reporte_movimientos(request.user, request.query_params)
        return respuesta_archivo(exportacion_mod.exportar_movimientos(reporte, formato))

    def _auxiliar_id(self, qp, *nombres):
        # A diferencia de ``_report_to_int``, un id no numérico es 400: ignorarlo
        # regresaba el kardex de todos los almacenes (#805).
        for nombre in nombres:
            raw = (qp.get(nombre) or "").strip()
            if raw:
                try:
                    valor = int(raw)
                except ValueError:
                    valor = 0
                # 0 o negativo también es 400: tratarlo como ausente quitaba el filtro (#815).
                if valor <= 0:
                    raise ValidationError({nombre: "Debe ser un id numérico mayor a 0."})
                return valor
        return None

    @extend_schema(
        parameters=[
            OpenApiParameter("q", str, description="SKU, SKU de producción o descripción."),
            OpenApiParameter("producto_variante", int, description="Id de la variante (salta la búsqueda)."),
            OpenApiParameter("almacen", int, description="Solo ese almacén."),
            OpenApiParameter("fecha_inicio", OpenApiTypes.DATE, description="YYYY-MM-DD; va con fecha_final."),
            OpenApiParameter("fecha_final", OpenApiTypes.DATE, description="YYYY-MM-DD; va con fecha_inicio."),
        ],
        responses={
            200: inline_serializer(
                "AuxiliarMovimientos",
                {
                    "variante": inline_serializer("AuxiliarOpcion", {
                        "producto_variante_id": drf_serializers.IntegerField(),
                        "sku": drf_serializers.CharField(),
                        "sku_produccion": drf_serializers.CharField(allow_null=True),
                        "descripcion": drf_serializers.CharField(allow_null=True),
                        "color": drf_serializers.CharField(allow_null=True),
                        "talla": drf_serializers.CharField(allow_null=True),
                        "activo": drf_serializers.BooleanField(),
                    }, allow_null=True),
                    "opciones": drf_serializers.ListField(child=drf_serializers.DictField()),
                    "hay_mas": drf_serializers.BooleanField(),
                    "resumen": drf_serializers.DictField(required=False),
                    "results": drf_serializers.ListField(child=drf_serializers.DictField(), required=False),
                },
            ),
            400: OpenApiResponse(description="Validación"),
            404: OpenApiResponse(description="Variante fuera de alcance"),
        },
    )
    @action(detail=False, methods=["get"], url_path="auxiliar")
    def auxiliar(self, request):
        """Auxiliar de movimientos (kardex) de una variante. Ver ``auxiliar.py``."""
        qp = request.query_params
        variante_id = self._auxiliar_id(qp, "producto_variante", "producto_variante_id")
        almacen_id = self._auxiliar_id(qp, "almacen", "almacen_id")
        q = (qp.get("q") or "").strip()
        if not variante_id:
            if not q:
                raise ValidationError({"q": "Manda q (SKU o descripción) o producto_variante."})
            encontradas = auxiliar_mod.buscar_variantes(request.user, q)
            if len(encontradas) != 1:
                return Response({
                    "variante": None,
                    "opciones": [auxiliar_mod.opcion(v) for v in encontradas[:auxiliar_mod.LIMITE_OPCIONES]],
                    "hay_mas": len(encontradas) > auxiliar_mod.LIMITE_OPCIONES,
                })
            variante = encontradas[0]
        else:
            variante = (
                auxiliar_mod.variantes_scope(request.user)
                .annotate(sku_produccion=auxiliar_mod.sku_produccion_subquery())
                .filter(pk=variante_id)
                .values("id", "sku", "nombre", "producto__nombre", "color__nombre", "talla__nombre", "activo", "sku_produccion")
                .first()
            )
            if not variante:
                return Response({"detail": "No encontrado."}, status=status.HTTP_404_NOT_FOUND)

        inicio_dt = final_dt = None
        if qp.get("fecha_inicio") or qp.get("fecha_final"):
            _fi, _ff, inicio_dt, final_dt = self._movement_report_parse_dates(request)
        # Mismo alcance que los reportes hermanos: empresa + sucursales (#800).
        # Con ``almacen`` fuera de alcance el conjunto queda vacío: sin datos.
        alcance = None
        if not request.user.is_superuser:
            almacenes, _almacen = self._movement_report_allowed_almacen_ids(request)
            alcance = (almacenes, request.user.sucursales_permitidas())
        limite = min(
            self._auxiliar_id(qp, "limite") or auxiliar_mod.LIMITE_RENGLONES, auxiliar_mod.LIMITE_RENGLONES_MAX
        )
        data = auxiliar_mod.kardex(request.user, variante["id"], almacen_id, inicio_dt, final_dt, alcance, limite)
        return Response({"variante": auxiliar_mod.opcion(variante), "opciones": [], "hay_mas": False, **data})

    def _empresa_ids_permitidas(self, user):
        """Empresas visibles para el usuario: solo la activa (#538).

        Mismo criterio que ``_movement_report_allowed_almacen_ids()`` en esta
        misma clase y que el resto de ViewSets del módulo. ``AuditoriaEvento``
        no tiene FK a ``sucursal``, así que aquí el scope sólo puede llegar
        hasta la empresa (los reportes sí acotan por sucursal, porque bajan al
        almacén).
        """
        empresa_ids = empresa_ids_lectura(user)
        # A diferencia de sus ViewSets hermanos, esta clase no declara el
        # atributo ``queryset``, así que drf-spectacular resuelve el modelo
        # llamando a ``get_queryset()`` con un request mock cuyo usuario es
        # ``AnonymousUser`` —sin M2M ``empresas``—. Sin este guard el esquema
        # se degrada (y cualquier llamador futuro sin usuario autenticado
        # reventaría); un principal sin M2M no aporta empresas y cae en el
        # ``none()`` del llamador.
        # Solo la empresa activa, como el resto del proyecto (#538): el M2M
        # ``empresas`` mostraba movimientos de otras empresas.
        return empresa_ids

    def get_queryset(self):
        user = self.request.user
        qs = self._base_queryset()
        qp = self.request.query_params

        # El aislamiento multi-tenant vive AQUÍ, no en la permission class:
        # ``IsAuthenticatedAndScoped`` sólo cierra las escrituras y deja pasar
        # toda lectura autenticada. Es la capa que usa el resto del proyecto.
        # El superusuario se evalúa PRIMERO —igual que ``AlmacenViewSet``,
        # ``PedidoViewSet`` o ``EmpleadoViewSet``—: ve todo aunque tenga una
        # empresa asignada. Sin empresa no se ve nada: se falla cerrado.
        if not user.is_superuser:
            empresa_ids = self._empresa_ids_permitidas(user)
            if not empresa_ids:
                return qs.none()
            qs = qs.filter(empresa_id__in=empresa_ids)

        # ``empresa_id`` es un filtro ADICIONAL dentro del universo ya acotado
        # arriba, nunca la fuente del tenant: antes el scope salía sólo de este
        # param y omitirlo devolvía los eventos —con sus ``antes_json``/
        # ``despues_json``— de todas las empresas. Mismo shape que
        # ``_build_report_almacenes_queryset()``.
        try:
            empresa_id = int(qp.get("empresa_id") or qp.get("empresa") or 0)
        except Exception:
            empresa_id = 0
        if empresa_id:
            qs = qs.filter(empresa_id=empresa_id)
        accion = (qp.get("accion") or qp.get("tipo") or "").strip().upper()
        if accion in {"ENTRADA", "SALIDA", "AJUSTE"}:
            qs = qs.filter(accion=accion)
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        limit_raw = (request.query_params.get("limit") or "").strip()
        try:
            limit = int(limit_raw) if limit_raw else 200
        except Exception:
            limit = 200
        limit = max(1, min(limit, 2000))

        serializer = self.get_serializer(queryset[:limit], many=True)
        return Response(serializer.data)

    @action(
        detail=False,
        methods=["get"],
        url_path=r"formal/(?P<movimiento_id>[0-9]+)/detalles",
    )
    def detalles_formal(self, request, movimiento_id=None):
        """Detalle de un ``MovimientoInventario`` (el ``movimiento_inventario_id``
        que devuelve ``reporte-movimientos-periodo``).

        ``{id}/detalles/`` busca por ``AuditoriaEvento.id_evento``: mandarle el id
        de un movimiento formal no lo encuentra (400) o, peor, abre otro evento.
        Mismo scope que ``MovimientoInventarioViewSet`` (empresa + sucursal).
        """
        user = request.user
        qs = MovimientoInventario.objects.filter(activo=True).select_related(
            "empresa", "sucursal", "usuario", "pedido", "recepcion",
            "transferencia", "op", "ajuste_inventario",
        )
        if not user.is_superuser:
            empresa_ids = self._empresa_ids_permitidas(user)
            sucursal_ids = list(user.sucursales_permitidas())
            qs = qs.filter(empresa_id__in=empresa_ids, sucursal_id__in=sucursal_ids)
        mov = qs.filter(pk=movimiento_id).first()
        if not mov:
            return Response(
                {"movimiento": "Movimiento no encontrado."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Sin ubicación, el almacén de cada lado sale con la misma precedencia
        # que el reporte por periodo (``detalle.almacen``, recepción, ajuste,
        # transferencia): la fila del reporte y su detalle dicen el mismo
        # almacén (EC-820). Solo llena los null.
        detalles_qs = mov.detalles.select_related(
            "producto", "producto_variante", "ubicacion_origen__almacen",
            "ubicacion_destino__almacen",
        )
        if mov.tipo_movimiento in reportes_mod.RUTAS_ALMACEN_POR_LADO:
            detalles_qs = reportes_mod.anotar_almacenes(detalles_qs, mov.tipo_movimiento, campos=())
        detalle = []
        for d in detalles_qs:
            origen, destino = d.ubicacion_origen, d.ubicacion_destino
            almacen_origen_id = getattr(origen, "almacen_id", None) or getattr(d, "almacen_origen_res", None)
            almacen_destino_id = getattr(destino, "almacen_id", None) or getattr(d, "almacen_destino_res", None)
            detalle.append(
                {
                    "id": d.pk,
                    "producto_id": d.producto_id,
                    "producto_variante_id": d.producto_variante_id,
                    "producto_nombre": (
                        getattr(d.producto_variante, "nombre", None)
                        or getattr(d.producto, "nombre", None)
                    ),
                    "ubicacion_origen_id": d.ubicacion_origen_id,
                    "ubicacion_origen_nombre": str(origen) if origen else None,
                    "almacen_origen_id": almacen_origen_id,
                    "ubicacion_destino_id": d.ubicacion_destino_id,
                    "ubicacion_destino_nombre": str(destino) if destino else None,
                    "almacen_destino_id": almacen_destino_id,
                    "cantidad": str(d.cantidad),
                    "costo_unitario": str(d.costo_unitario),
                }
            )

        usuario = mov.usuario
        return Response(
            {
                "id": mov.pk,
                "tipo_movimiento": mov.tipo_movimiento,
                "fecha": mov.fecha_movimiento,
                "usuario": usuario.pk,
                "usuario_nombre": usuario.get_full_name().strip() or usuario.email,
                "sucursal_id": mov.sucursal_id,
                "sucursal_nombre": mov.sucursal.nombre,
                "empresa_id": mov.empresa_id,
                "empresa_nombre": mov.empresa.razon_social,
                "pedido_id": mov.pedido_id,
                "recepcion_id": mov.recepcion_id,
                "transferencia_id": mov.transferencia_id,
                "op_id": mov.op_id,
                "ajuste_inventario_id": mov.ajuste_inventario_id,
                "observaciones": mov.observaciones,
                "detalle_count": len(detalle),
                "detalle": detalle,
            }
        )

    @action(detail=True, methods=["get"], url_path="detalles")
    def detalles(self, request, pk=None):
        movimiento = self.get_queryset().filter(id_evento=pk).first()
        if not movimiento:
            raise ValidationError({"movimiento": "Movimiento no encontrado."})

        before_items = (movimiento.antes_json or {}).get("items") or []
        after_items = (movimiento.despues_json or {}).get("items") or []
        items = after_items or before_items

        despues = movimiento.despues_json or {}
        almacen_id = despues.get("almacen_id")
        sucursal_id = despues.get("sucursal_id")

        def _safe_int(v):
            try:
                return int(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        # Single lookups for top-level names (one record — no N+1 risk)
        almacen_obj = (
            Almacen.objects.filter(pk=almacen_id).only("nombre").first()
            if almacen_id else None
        )
        sucursal_obj = (
            Sucursal.objects.filter(pk=sucursal_id).only("nombre").first()
            if sucursal_id else None
        )
        empresa_nombre = getattr(movimiento.empresa, "razon_social", None)

        # Collect IDs from all items for a single bulk fetch each (avoids N+1)
        variante_ids, producto_ids, ubicacion_ids = set(), set(), set()
        for it in items:
            if not isinstance(it, dict):
                continue
            v = _safe_int(it.get("producto_variante_id") or it.get("producto_variante"))
            p = _safe_int(it.get("producto_id") or it.get("producto"))
            u = _safe_int(it.get("ubicacion_id") or it.get("ubicacion"))
            if v:
                variante_ids.add(v)
            if p:
                producto_ids.add(p)
            if u:
                ubicacion_ids.add(u)

        variantes = {
            obj.pk: obj
            for obj in ProductoVariante.objects.filter(pk__in=variante_ids).only("id", "nombre")
        } if variante_ids else {}
        productos = {
            obj.pk: obj
            for obj in Producto.objects.filter(pk__in=producto_ids).only("id", "nombre")
        } if producto_ids else {}
        ubicaciones = {
            obj.pk: obj
            for obj in Ubicacion.objects.select_related("almacen").filter(pk__in=ubicacion_ids)
        } if ubicacion_ids else {}

        enriched_items = []
        for it in items:
            if not isinstance(it, dict):
                enriched_items.append(it)
                continue
            item = dict(it)
            v_id = _safe_int(it.get("producto_variante_id") or it.get("producto_variante"))
            p_id = _safe_int(it.get("producto_id") or it.get("producto"))
            u_id = _safe_int(it.get("ubicacion_id") or it.get("ubicacion"))

            if v_id and v_id in variantes:
                item["producto_nombre"] = variantes[v_id].nombre
            elif p_id and p_id in productos:
                item["producto_nombre"] = productos[p_id].nombre
            else:
                item["producto_nombre"] = None

            item["ubicacion_nombre"] = (
                str(ubicaciones[u_id]) if u_id and u_id in ubicaciones else None
            )
            enriched_items.append(item)

        return Response(
            {
                "id": movimiento.id_evento,
                "tipo_movimiento": movimiento.accion,
                "fecha": movimiento.created_at,
                "usuario": getattr(movimiento.usuario, "pk", None),
                "usuario_nombre": (
                    movimiento.usuario.get_full_name().strip() or movimiento.usuario.email
                    if getattr(movimiento, "usuario", None)
                    else None
                ),
                "almacen_id": almacen_id,
                "almacen_nombre": getattr(almacen_obj, "nombre", None),
                "sucursal_id": sucursal_id,
                "sucursal_nombre": getattr(sucursal_obj, "nombre", None),
                "empresa_id": movimiento.empresa_id,
                "empresa_nombre": empresa_nombre,
                "detalle_count": len(enriched_items),
                "detalle": enriched_items,
                "antes_json": movimiento.antes_json,
                "despues_json": movimiento.despues_json,
            },
            status=status.HTTP_200_OK,
        )

class MovimientoInventarioViewSet(viewsets.ModelViewSet):
    queryset = MovimientoInventario.objects.filter(activo=True).select_related('empresa', 'sucursal', 'pedido', 'entrega', 'devolucion', 'ajuste_inventario')
    serializer_class = MovimientoInventarioSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        
        empresa_ids = empresa_ids_lectura(user)  # solo la empresa activa (EC-816)
        sucursal_ids = list(user.sucursales_permitidas())
        
        return qs.filter(
            models.Q(empresa_id__in=empresa_ids) &
            models.Q(sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_superuser:
            sucursal = serializer.validated_data.get('sucursal')
            empresa = serializer.validated_data.get('empresa')

            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if empresa.pk != user.empresa_id:  # sin empresa activa no se escribe (EC-816)
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()

    def perform_destroy(self, instance):
        instance.soft_delete()


class MovimientoInventarioDetalleViewSet(viewsets.ModelViewSet):
    queryset = MovimientoInventarioDetalle.objects.all().select_related('movimiento_inventario', 'producto', 'ubicacion_origen', 'ubicacion_destino', 'lote', 'serie')
    serializer_class = MovimientoInventarioDetalleSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        
        empresa_ids = empresa_ids_lectura(user)  # solo la empresa activa (EC-816)
        sucursal_ids = list(user.sucursales_permitidas())
        
        return qs.filter(
            models.Q(movimiento_inventario__empresa_id__in=empresa_ids) &
            models.Q(movimiento_inventario__sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_superuser:
            movimiento = serializer.validated_data.get('movimiento_inventario')
            if movimiento:
                if movimiento.sucursal_id and not user.sucursales.filter(pk=movimiento.sucursal_id).exists():
                    raise PermissionDenied("No tiene acceso a la sucursal del movimiento de inventario")
                if movimiento.empresa_id:
                    if movimiento.empresa_id != user.empresa_id:  # sin empresa activa no se escribe (EC-816)
                        raise PermissionDenied("No tiene acceso a la empresa del movimiento de inventario")
        serializer.save()

class AjusteInventarioViewSet(viewsets.ModelViewSet):
    queryset = AjusteInventario.objects.filter(activo=True).select_related('empresa', 'sucursal', 'almacen')
    serializer_class = AjusteInventarioSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        
        empresa_ids = empresa_ids_lectura(user)  # solo la empresa activa (EC-816)
        sucursal_ids = list(user.sucursales_permitidas())
        
        return qs.filter(
            models.Q(empresa_id__in=empresa_ids) &
            models.Q(sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_superuser:
            sucursal = serializer.validated_data.get('sucursal')
            empresa = serializer.validated_data.get('empresa')
            
            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if empresa.pk != user.empresa_id:  # sin empresa activa no se escribe (EC-816)
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()
    
    def perform_destroy(self, instance):
        instance.soft_delete()
