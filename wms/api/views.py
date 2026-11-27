from django.db.models import Prefetch
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from wms.api.serializers import (
    DespachoCreateSerializer,
    DespachoSerializer,
    EtiquetaRFIDCreateSerializer,
    EtiquetaRFIDSerializer,
    PackingCreateSerializer,
    TransferenciaListSerializer,
    TransferenciaRetrieveSerializer,
    TransferenciaSerializer,
    PickingCreateSerializer,
    PickingTrasladoSerializer,
    PickingSerializer,
    PickingListSerializer,
    PackingSerializer,
)
from wms.models import (
    Despacho,
    DespachoDetalle,
    EtiquetaRFIDDetalle,
    EtiquetaRFIDImpresion,
    Packing,
    PackingDetalle,
    Picking,
    PickingDetalle,
    PickingOrdenTrabajo,
    RfidLecturaUltima,
    RfidScan,
    Transferencia,
    TransferenciaDetalle,
)
from nucleo.kpis import entero_param
from nucleo.listados import PaginacionOpcionalLimitOffset
from wms.api.lectores_rfid import lectores_visibles
from wms.api import pickings_listado
from wms.scope import documentos_wms_visibles
from wms.services import cancelacion_service
from wms.services.despacho_service import DespachoService
from wms.services.rfid_scan_service import (
    lector_desde_request,
    lectores_estado,
    normalizar_epc,
    recibir_lecturas,
    variantes_epc,
)
from wms.services.transferencia_service import TransferenciaService
from wms.services.picking_service import PickingService
from wms.services.packing_service import PackingService
from wms.services.rfid_label_service import RFIDLabelService


class _ReleerAltaMixin:
    """``_releer``: el documento recién creado, leído con las precargas de ``get_queryset``.

    Lo comparten ``PickingViewSet``, ``PackingViewSet`` y ``DespachoViewSet``. Serializar la instancia
    que devuelve el servicio resolvía cada FK de cada renglón con su propia
    consulta; releerla deja la respuesta del alta con el mismo costo fijo que el
    retrieve (EC-882), como ya hace ``trasladar``. El servicio ya validó empresa y
    sucursal, así que el documento está en alcance.
    """

    def _releer(self, documento):
        return self.get_queryset().get(pk=documento.pk)


class TransferenciaViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet
):
    queryset = Transferencia.objects.all()
    serializer_class = TransferenciaSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related(
                "almacen_origen", "almacen_destino", "usuario", "cancelado_por", "reversa", "reversa_de"
            )
            .order_by("-fecha_creacion", "-id")
        )

        # Solo el retrieve individual anida ``transferencia_detalle``: los renglones
        # se traen con sus FK en un único prefetch para evitar el N+1 que tendría el
        # shape anidado. ``ubicacion_*__almacen`` viaja en el select_related porque la
        # etiqueta de una Ubicacion se compone con ``almacen.nombre``. El list no lo
        # necesita y se mantiene ligero.
        if self.action == "retrieve":
            qs = qs.prefetch_related(
                Prefetch(
                    "transferencia_detalle",
                    queryset=TransferenciaDetalle.objects.select_related(
                        "producto",
                        "producto_variante",
                        "producto_variante__color",
                        "producto_variante__talla",
                        "variante_produccion__color",
                        "variante_produccion__talla",
                        "ubicacion_origen",
                        "ubicacion_origen__almacen",
                        "ubicacion_destino",
                        "ubicacion_destino__almacen",
                    ).order_by("id"),
                )
            )

        # Aislamiento multi-tenant (``wms/scope.py``): empresa activa, superusuario
        # incluido (EC-882). Dentro de la empresa, los no admin solo ven las
        # sucursales de ``sucursales_permitidas()`` —el mismo conjunto que
        # ``TransferenciaService.handle_store()`` exige al escribir—, filtradas por
        # ``Transferencia.sucursal`` (la dueña), no por la de los almacenes.
        return documentos_wms_visibles(qs, user)

    def get_serializer_class(self):
        # retrieve → encabezado + renglones anidados; list → forma plana ligera;
        # el resto (create) conserva la forma original de escritura.
        if self.action == "retrieve":
            return TransferenciaRetrieveSerializer
        if self.action == "list":
            return TransferenciaListSerializer
        return TransferenciaSerializer

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        res = TransferenciaService.handle_store(serializer.validated_data, request.user, request=request)
        return Response(TransferenciaSerializer(res).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="revertir", url_name="revertir")
    def revertir(self, request, pk=None):
        """Crea la TR inversa (folio propio, ``reversa_de``) y deja la original CANCELADA.
        Body ``{"motivo": "..."}`` (obligatorio). Admin de empresa, superuser o permiso
        ``WMS-CANCELAR``. Otra empresa: 404. No borra: el documento queda cancelado con su folio.
        TR de un picking (cancelado): sólo la última viva de su cadena, y el picking queda en
        el origen de la TR revertida (EC-949)."""
        tr = self.get_object()
        cancelacion_service.exigir_permiso(request.user)
        motivo = cancelacion_service.motivo_de(request.data)
        original, reversa = cancelacion_service.revertir_transferencia(tr, request, motivo)
        return Response({
            "original": TransferenciaListSerializer(self.get_queryset().get(pk=original.pk)).data,
            "reversa": TransferenciaListSerializer(self.get_queryset().get(pk=reversa.pk)).data,
        }, status=status.HTTP_201_CREATED)

@extend_schema_view(
    list=extend_schema(parameters=[
        pickings_listado.PARAMETRO_RESUMEN_OPENAPI,
        *pickings_listado.PARAMETROS_FILTRO_OPENAPI,
        pickings_listado.PARAMETRO_ORDERING_OPENAPI,
    ]),
)
class PickingViewSet(_ReleerAltaMixin, mixins.RetrieveModelMixin, mixins.ListModelMixin, GenericViewSet):
    """#1149: ``?limit=&offset=`` pagina (sin ``limit``, la lista completa como
    antes) y ``?resumen=1`` regresa el listado sin renglones anidados.

    EC-921 (modo servidor del listado): ``?estado=``, ``?prioridad=``, ``?search=`` y
    ``?ordering=`` (``wms/api/pickings_listado.py``), y ``kpis/`` con los mismos
    filtros. Sin parámetros, la misma respuesta de siempre."""

    queryset = Picking.objects.all()
    serializer_class = PickingSerializer
    pagination_class = PaginacionOpcionalLimitOffset
    # Sin ``OrderingFilter`` de DRF: sin lista blanca aceptaba cualquier campo del
    # serializer, sin desempate, y ``prioridad`` salía alfabética (ALTA, BAJA, MEDIA).
    # ``SearchFilter`` sólo en el listado (``BusquedaPickingsFilter``).
    filter_backends = [
        *[b for b in GenericViewSet.filter_backends if not issubclass(b, (OrderingFilter, SearchFilter))],
        pickings_listado.BusquedaPickingsFilter,
        pickings_listado.OrdenamientoPickingsFilter,
    ]
    # Lo que busca la caja del front ("folio, pedido u operador"); el operador por el
    # nombre que pinta la fila (el correo sólo si no tiene nombre).
    search_fields = [
        "folio", "pedido__folio", pickings_listado.CAMPO_BUSQUEDA_OPERADOR,
        "pedido__cliente__nombre", "pedido__cliente_nombre",
    ]
    # Columnas de la tabla con el nombre que usa el front (``pedido_folio``,
    # ``operador_nombre``) y ``prioridad`` por urgencia (``OrdenamientoPickingsFilter``).
    ordering_fields = list(pickings_listado.CAMPOS_ORDEN)

    def _resumen(self):
        # ``?vista=lista`` (nombre del front) = ``?resumen=1`` (#1149).
        params = self.request.query_params
        return self.action == "list" and (
            str(params.get("resumen", "")).lower() in {"1", "true", "si"} or params.get("vista") == "lista"
        )

    def get_queryset(self):
        user = self.request.user
        # Las FK del encabezado que PickingSerializer resuelve a nombre viajan en
        # un solo select_related (oleada/zona_almacen/lote solo usan campos
        # locales para su etiqueta, no hace falta profundizar más).
        #
        # A diferencia de TransferenciaViewSet (que solo anida renglones en el
        # retrieve), PickingSerializer es compartido y anida ``picking_detalle``
        # también en el list, así que el prefetch aplica a ambas acciones.
        # ``ubicacion__almacen`` viaja en el select_related porque la etiqueta de
        # una Ubicacion se compone con ``almacen.nombre``.
        qs = (
            super()
            .get_queryset()
            .select_related(
                "pedido",
                "pedido__cliente",
                "operador",
                "almacen",
                "almacen_destino",
                "transferencia",
                "usuario",
                "oleada",
                "zona_almacen",
                "lote",
                "cancelado_por",
            )
            .prefetch_related(
                Prefetch(
                    "picking_detalle",
                    queryset=PickingDetalle.objects.select_related(
                        "producto",
                        "producto_variante",
                        "pedido_detalle_talla",
                        "pedido_detalle_talla__talla",
                        "producto_variante__talla",
                        "variante_produccion__talla",
                        "variante_produccion__color",
                        "producto_variante__color",
                        "ubicacion",
                        "ubicacion__almacen",
                        "operador",
                    ).order_by("id"),
                ),
                Prefetch(
                    "ordenes_trabajo",
                    queryset=PickingOrdenTrabajo.objects.select_related(
                        "orden_bordado",
                        "orden_reflejante",
                        "orden_corte_manga",
                    ).order_by("id"),
                ),
            )
            .order_by("-created_at", "-id")
        )
        if self._resumen():
            qs = qs.prefetch_related(None)

        # Aislamiento multi-tenant (``wms/scope.py``): empresa activa, superusuario
        # incluido (EC-882). Los no admin ven las sucursales de
        # ``sucursales_permitidas()``, el mismo conjunto que une el onboarding
        # (``PickingService.onboarding_payload``): quien crea un picking lo vuelve a
        # ver en list/retrieve. ``trasladar`` pasa por aquí vía ``get_object``.
        qs = documentos_wms_visibles(qs, user)
        # Filtros del listado (EC-921) encima del alcance y sólo donde se listan
        # filas: en el detalle un parámetro suelto daría 404 sobre un picking visible.
        if self.action in pickings_listado.ACCIONES_FILTRADAS:
            qs = pickings_listado.filtrar(self.request, qs)
        return qs

    def list(self, request, *args, **kwargs):
        # Empaque y surtido por programación de toda la página en bloque
        # (``pickings_listado.contexto_filas``), no por fila en el serializer.
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        filas = [*(page if page is not None else queryset)]
        serializer = self.get_serializer(filas, many=True)
        serializer.context["contexto_filas"] = pickings_listado.contexto_filas(filas)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    def get_serializer_class(self):
        if self.action in {"create", "onboarding"} and self.request.method == "POST":
            return PickingCreateSerializer
        if self._resumen():
            return PickingListSerializer
        return PickingSerializer

    @extend_schema(
        summary="KPIs del listado de pickings con sus filtros",
        description=(
            "Mismos `estado`, `prioridad` y `search` que el listado, sin paginar ni ordenar "
            "(`limit`, `offset` y `ordering` se ignoran). Respuesta: `generado_en`, `total` "
            "(= `count` del listado con los mismos filtros), `lineas_por_surtir` (suma de "
            "`total_lineas - total_lineas_completas`, cada picking acotado a >= 0) y "
            "`por_prioridad` (`[{prioridad, prioridad_label, total}]`, ALTA, MEDIA, BAJA)."
        ),
        parameters=[*pickings_listado.PARAMETROS_FILTRO_OPENAPI],
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"], url_path=pickings_listado.ACCION_KPIS)
    def kpis(self, request):
        """Tarjetas de ``PickingView`` sobre el conjunto filtrado, en una consulta
        (``pickings_listado.kpis``). Mismo ``get_queryset`` (alcance y filtros) y los
        mismos filter backends que el listado, como ``PedidoViewSet.kpis_mesa``; el
        orden lo descarta ``pickings_listado.kpis`` antes de agregar."""
        qs = self.filter_queryset(self.get_queryset())
        return Response({"generado_en": timezone.now(), **pickings_listado.kpis(qs)})

    @extend_schema(
        summary="Indicadores del listado de surtidos con sus filtros",
        description=(
            "Mismos filtros y `search` que el listado, sobre todo el conjunto (sin paginar). "
            "`por_empacar` {surtidos, piezas}, `surtidos_hoy` {fecha, surtidos, piezas, pedidos} "
            "(hora de México) y `piezas_por_destino` [{destino, piezas}]. Cancelados no cuentan."
        ),
        parameters=[*pickings_listado.PARAMETROS_FILTRO_OPENAPI],
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"], url_path=pickings_listado.ACCION_RESUMEN)
    def resumen(self, request):
        qs = self.filter_queryset(self.get_queryset()).prefetch_related(None)
        return Response({"generado_en": timezone.now(), **pickings_listado.resumen(qs)})

    @extend_schema(
        summary="Catálogos de los filtros del listado de surtidos",
        description="Clientes y operadores con al menos un surtido visible: `{clientes: [{id, nombre}], operadores: [{id, nombre}]}`.",
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"], url_path="filtros")
    def filtros(self, request):
        return Response(pickings_listado.catalogos_filtros(self.get_queryset().prefetch_related(None)))


    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        if request.method == "GET":
            pedido_id = request.query_params.get("pedido") or request.query_params.get("pedido_id")
            almacen_origen_id = request.query_params.get("almacen_origen") or request.query_params.get("almacen_origen_id")
            almacen_destino_id = request.query_params.get("almacen_destino") or request.query_params.get("almacen_destino_id")
            payload = PickingService.onboarding_payload(
                request.user,
                pedido_id=pedido_id,
                almacen_origen_id=almacen_origen_id,
                almacen_destino_id=almacen_destino_id,
                limite_pedidos=request.query_params.get("pedidos_limit"),
            )
            return Response(payload)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        picking, ordenes_trabajo = PickingService.handle_store(serializer.validated_data, request.user)
        response_data = PickingSerializer(self._releer(picking)).data
        response_data["ordenes_trabajo_generadas"] = ordenes_trabajo
        return Response(response_data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="trasladar", url_name="trasladar")
    def trasladar(self, request, pk=None):
        """Mueve el picking a otro almacén (p. ej. de bordados a APARTADOS)."""
        picking = self.get_object()  # otra empresa: 404 antes que 403
        if not request.user.tiene_permiso("C-WMS-PICKING"):  # mueve inventario (#695)
            raise PermissionDenied("No tienes permiso para trasladar pickings.")
        serializer = PickingTrasladoSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        picking = PickingService.trasladar(
            picking,
            serializer.validated_data["almacen"],
            request.user,
            serializer.validated_data.get("observaciones"),
        )
        picking = self.get_queryset().get(pk=picking.pk)
        return Response(PickingSerializer(picking).data)

    @action(detail=True, methods=["post"], url_path="cancelar", url_name="cancelar")
    def cancelar(self, request, pk=None):
        """Cancela un picking sin packing vivo ni órdenes de trabajo vivas; no mueve inventario.
        Body ``{"motivo": "..."}`` (obligatorio). Admin de empresa, superuser o permiso
        ``WMS-CANCELAR``. Otra empresa: 404. No borra: el documento queda cancelado con su folio."""
        picking = self.get_object()
        cancelacion_service.exigir_permiso(request.user)
        motivo = cancelacion_service.motivo_de(request.data)
        cancelacion_service.cancelar_picking(picking, request, motivo)
        return Response(PickingSerializer(self.get_queryset().get(pk=picking.pk)).data)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        picking, ordenes_trabajo = PickingService.handle_store(serializer.validated_data, request.user)
        response_data = PickingSerializer(self._releer(picking)).data
        response_data["ordenes_trabajo_generadas"] = ordenes_trabajo
        return Response(response_data, status=status.HTTP_201_CREATED)

class PackingViewSet(_ReleerAltaMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    queryset = Packing.objects.all()
    serializer_class = PackingSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related(
                "pedido", "pedido__cliente", "picking", "picking__almacen", "operador", "usuario",
                "cancelado_por",
            )
            .prefetch_related(
                Prefetch(
                    "packing_detalle",
                    queryset=PackingDetalle.objects.select_related(
                        "caja",
                        "picking_detalle",
                        "picking_detalle__producto",
                        "picking_detalle__producto_variante",
                        "picking_detalle__pedido_detalle_talla",
                        "picking_detalle__pedido_detalle_talla__talla",
                        "picking_detalle__producto_variante__talla",
                        "picking_detalle__variante_produccion__talla",
                        "picking_detalle__variante_produccion__color",
                        "picking_detalle__producto_variante__color",
                        "picking_detalle__ubicacion",
                        "picking_detalle__ubicacion__almacen",
                    ).order_by("id"),
                )
            )
            .order_by("-created_at", "-id")
        )

        # Mismo alcance que picking y transferencias (``wms/scope.py``, EC-882).
        return documentos_wms_visibles(qs, user)

    def get_serializer_class(self):
        if self.action in {"create", "onboarding"} and self.request.method == "POST":
            return PackingCreateSerializer
        return PackingSerializer

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        if request.method == "GET":
            picking_id = request.query_params.get("picking") or request.query_params.get(
                "picking_id"
            )
            payload = PackingService.onboarding_payload(
                request.user,
                picking_id=picking_id,
            )
            return Response(payload)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        packing_instance = PackingService.handle_store(serializer.validated_data, request.user)
        return Response(PackingSerializer(self._releer(packing_instance)).data, status=status.HTTP_201_CREATED)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        packing_instance = PackingService.handle_store(serializer.validated_data, request.user)
        return Response(PackingSerializer(self._releer(packing_instance)).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="cancelar", url_name="cancelar")
    def cancelar(self, request, pk=None):
        """Cancela un packing sin despacho activo; no mueve inventario.
        Body ``{"motivo": "..."}`` (obligatorio). Admin de empresa, superuser o permiso
        ``WMS-CANCELAR``. Otra empresa: 404. No borra: el documento queda cancelado con su folio."""
        packing = self.get_object()
        cancelacion_service.exigir_permiso(request.user)
        motivo = cancelacion_service.motivo_de(request.data)
        cancelacion_service.cancelar_packing(packing, request, motivo)
        return Response(PackingSerializer(self.get_queryset().get(pk=packing.pk)).data)


class DespachoViewSet(_ReleerAltaMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    queryset = Despacho.objects.all()
    serializer_class = DespachoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related(
                "packing",
                "packing__pedido",
                "packing__pedido__cliente",
                "packing__sucursal",
                "envio",
                "envio__transportista",
                "chofer",
                "usuario",
                "cancelado_por",
                "entregado_por",
            )
            .prefetch_related(
                Prefetch(
                    "despacho_detalle",
                    queryset=DespachoDetalle.objects.select_related(
                        "packing_detalle",
                        "packing_detalle__caja",
                        "packing_detalle__picking_detalle",
                        "packing_detalle__picking_detalle__producto",
                        "packing_detalle__picking_detalle__producto_variante",
                        "packing_detalle__picking_detalle__pedido_detalle_talla",
                        "packing_detalle__picking_detalle__pedido_detalle_talla__variante",
                        "packing_detalle__picking_detalle__pedido_detalle_talla__variante__talla",
                        "packing_detalle__picking_detalle__pedido_detalle_talla__variante__color",
                        "packing_detalle__picking_detalle__ubicacion",
                        "packing_detalle__picking_detalle__ubicacion__almacen",
                    ).order_by("id"),
                )
            )
            # ``Despacho`` no tiene fecha propia: el más reciente primero se
            # resuelve por la fecha de alta del ``Packing`` del que cuelga (ya
            # viene en el ``select_related``, así que no añade consultas).
            .order_by("-packing__created_at", "-id")
        )

        # Superusuario incluido: solo su empresa activa (EC-798, #1148).
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(packing__empresa=empresa)
        # ``?estado=ACTIVO|CANCELADO``; sin parámetro, todos (cada uno trae su ``estado``).
        estado = (self.request.query_params.get("estado") or "").upper()
        if self.action == "list" and estado in Despacho.Estado.values:
            qs = qs.filter(estado=estado)
        # ``?entregado=true|false``: con o sin ``fecha_entrega`` (#1436); otro valor -> 400.
        entregado = self.request.query_params.get("entregado")
        if self.action == "list" and entregado is not None:
            entregado = entregado.strip().lower()
            if entregado not in {"true", "false"}:
                raise ValidationError({"entregado": "Usa true o false."})
            qs = qs.filter(fecha_entrega__isnull=entregado == "false")
        if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(packing__sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        if self.action in {"create", "onboarding"} and self.request.method == "POST":
            return DespachoCreateSerializer
        return DespachoSerializer

    @action(detail=False, methods=["get", "post"], url_path="onboarding", url_name="onboarding")
    def onboarding(self, request):
        if request.method == "GET":
            packing_id = request.query_params.get("packing") or request.query_params.get(
                "packing_id"
            )
            payload = DespachoService.onboarding_payload(
                request.user,
                packing_id=packing_id,
            )
            return Response(payload)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        despacho_instance = DespachoService.handle_store(serializer.validated_data, request.user)
        return Response(DespachoSerializer(self._releer(despacho_instance)).data, status=status.HTTP_201_CREATED)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        despacho_instance = DespachoService.handle_store(serializer.validated_data, request.user)
        return Response(DespachoSerializer(self._releer(despacho_instance)).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="cancelar", url_name="cancelar")
    def cancelar(self, request, pk=None):
        """Cancela el despacho y regresa sus prendas al almacén con una ENTRADA que espeja
        su SALIDA (mismo costo). El packing vuelve a quedar por despachar. Body
        ``{"motivo": "..."}`` (obligatorio). Admin de empresa, superuser o permiso
        ``WMS-CANCELAR``. Otra empresa: 404."""
        despacho = self.get_object()
        cancelacion_service.exigir_permiso(request.user)
        motivo = cancelacion_service.motivo_de(request.data)
        cancelacion_service.cancelar_despacho(despacho, request, motivo)
        return Response(DespachoSerializer(self.get_queryset().get(pk=despacho.pk)).data)

    @action(detail=True, methods=["post"], url_path="entregar", url_name="entregar")
    def entregar(self, request, pk=None):
        """#1361: marca el despacho entregado. Body ``{"recibio": "...", "fecha_entrega": ISO}``
        (ambos opcionales; sin fecha, ahora). Permiso ``C-WMS-ENVIO``. Ya entregado no se cancela."""
        despacho = self.get_object()
        if not request.user.tiene_permiso("C-WMS-ENVIO"):
            raise PermissionDenied("No tienes permiso para esta acción (C-WMS-ENVIO).")
        fecha = None
        crudo = request.data.get("fecha_entrega") if hasattr(request.data, "get") else None
        if crudo:
            fecha = parse_datetime(str(crudo))
            if fecha is None:
                raise ValidationError({"fecha_entrega": "Fecha inválida (ISO 8601)."})
            if timezone.is_naive(fecha):
                fecha = timezone.make_aware(fecha)
        recibio = request.data.get("recibio") if hasattr(request.data, "get") else ""
        cancelacion_service.entregar_despacho(despacho, request, str(recibio or "").strip(), fecha)
        return Response(DespachoSerializer(self.get_queryset().get(pk=despacho.pk)).data)


TOPE_CONTEO_STATS = 10000


class EtiquetaRFIDViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    queryset = EtiquetaRFIDImpresion.objects.all()
    serializer_class = EtiquetaRFIDSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related("empresa", "sucursal", "usuario", "producto", "producto_variante")
            .prefetch_related(
                Prefetch(
                    "etiquetas",
                    queryset=EtiquetaRFIDDetalle.objects.order_by("id"),
                )
            )
            .order_by("-created_at", "-id")
        )

        # Superusuario incluido: solo su empresa activa (EC-798, #1148).
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        if self.action in {"create", "registrar_impresion", "onboarding"} and self.request.method == "POST":
            return EtiquetaRFIDCreateSerializer
        return EtiquetaRFIDSerializer

    @action(
        detail=False,
        methods=["get"],
        url_path="buscar",
        url_name="buscar",
    )
    def buscar(self, request):
        """Buscador simple de SKU/producto (mismo criterio que QA/imprimir_etiqueta).

        Scope: multi-tenant por empresa + sucursales permitidas.
        Filtra con OR por:
          - ProductoVariante: sku, nombre, producto.nombre, producto.codigo, producto.cod_proscai
          - Producto: nombre, codigo, cod_proscai

        Respuesta lista simple tipo QA:
        [{
          "tipo": "variante" | "producto",
          "id",
          "producto_variante_id" | null,
          "producto_id",
          "label": "10005032XC - CAMISA MANGA LARGA THAI PREMIUM · VINO · 2XC",
          "sku",
          "nombre",
          "color_nombre",
          "talla_nombre",
          "codigo",
          "cod_proscai"
        }]
        """
        from django.db.models import Q
        from catalogo.models import Producto, ProductoVariante

        user = request.user
        empresa = getattr(user, "empresa", None)
        q = (request.query_params.get("q") or "").strip()

        empty = {"q": q, "resultados": []}
        if empresa is None:
            return Response(empty)

        variantes_qs = (
            ProductoVariante.objects.select_related("producto", "color", "talla")
            .filter(empresa=empresa, activo=True)
            .order_by("sku")
        )
        productos_qs = Producto.objects.filter(empresa=empresa, activo=True).order_by(
            "nombre"
        )

        if q:
            variantes_qs = variantes_qs.filter(
                Q(sku__icontains=q)
                | Q(nombre__icontains=q)
                | Q(producto__nombre__icontains=q)
                | Q(producto__codigo__icontains=q)
                | Q(producto__cod_proscai__icontains=q)
            )
            productos_qs = productos_qs.filter(
                Q(nombre__icontains=q)
                | Q(codigo__icontains=q)
                | Q(cod_proscai__icontains=q)
            )

        sucursal_ids = (
            None
            if (getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False))
            else user.sucursales_permitidas()
        )
        resultados = []

        for v in variantes_qs[:30]:
            p = v.producto
            color_n = getattr(v.color, "nombre", None)
            talla_n = getattr(v.talla, "nombre", None)
            sec = " · ".join([x for x in [color_n, talla_n] if x])
            label_prefix = f"{v.sku} - " if v.sku else ""
            label = f"{label_prefix}{p.nombre}"
            if sec:
                label = f"{label} · {sec}"
            resultados.append({
                "tipo": "variante",
                "id": v.pk,
                "producto_variante_id": v.pk,
                "producto_id": p.pk,
                "label": label,
                "sku": v.sku,
                "nombre": p.nombre,
                "color_nombre": color_n,
                "talla_nombre": talla_n,
                "codigo": p.codigo,
                "cod_proscai": p.cod_proscai,
            })

        producto_ids_con_variantes = {item["producto_id"] for item in resultados}
        for p in productos_qs[:30]:
            if p.pk in producto_ids_con_variantes:
                continue
            codigo_impresion = p.codigo or p.cod_proscai or f"PROD-{p.pk}"
            resultados.append({
                "tipo": "producto",
                "id": p.pk,
                "producto_variante_id": None,
                "producto_id": p.pk,
                "label": f"{codigo_impresion} - {p.nombre}",
                "sku": None,
                "nombre": p.nombre,
                "color_nombre": None,
                "talla_nombre": None,
                "codigo": p.codigo,
                "cod_proscai": p.cod_proscai,
            })

        return Response({
            "q": q,
            "sucursal_ids": sucursal_ids,
            "resultados": resultados,
        })

    @action(
        detail=False,
        methods=["get", "post"],
        url_path="onboarding",
        url_name="onboarding",
    )
    def onboarding(self, request):
        """Onboarding para Impresión de Etiquetas RFID (1 modal, 1 URL).

        PATRÓN: mismo onboarding que WMS picking/packing/despacho / Producción
        orden bordado/reflejante/corte-manga.

        Qué hace internamente (le ahorra TODO este trabajo a Next.js):
          - GET vacío              =>  devuelve buscador (resultados sugeridos),
                                        sin preview (está abierto el modal sin
                                        seleccionar SKU).
          - GET ?q=XXXX            =>  corre BUSCADOR por texto (mismo filtro
                                        Q de QA / imprimir_etiqueta).
          - GET ?variante=X&cantidad=3
                    o ?producto=Y  =>  devuelve buscador + **PREVIEW COMPLETO
                                        CON TODOS LOS ZPL INDIVIDUALES YA ARMADOS
                                        POR CADA ETIQUETA** (el frontend **no**
                                        tiene que reconstruir ZPL ni reemplazar
                                        EPCs —sólo itera zpl_individual[] y
                                        se lo envía a Browser Print uno por uno).
          - POST                   =>  misma escritura que registrar-impresion:
                                        crea impresión + detalle EPCs, y así la
                                        lista ``/api/v1/wms/etiquetas-rfid/``
                                        deja de estar vacía.

        Autenticación: sesión/token del usuario; scope empresa + sucursales.
        """
        if request.method == "GET":
            user = request.user
            empresa = getattr(user, "empresa", None)

            q = (request.query_params.get("q") or "").strip()
            variante_id = request.query_params.get("variante") or request.query_params.get("variante_id")
            producto_id = request.query_params.get("producto") or request.query_params.get("producto_id")
            cantidad_raw = request.query_params.get("cantidad")
            rfid_mode_raw = request.query_params.get("rfid_mode", "true")
            cantidad = int(cantidad_raw) if (cantidad_raw and str(cantidad_raw).isdigit()) else 1
            rfid_mode = str(rfid_mode_raw).lower() not in {"0", "false", "no", "off"}

            buscar_payload = self.buscar(request).data
            if not isinstance(buscar_payload, dict):
                buscar_payload = {"q": q, "resultados": []}

            empty = {
                "q": q,
                "resultados": buscar_payload.get("resultados", []),
                "sucursal_ids": buscar_payload.get("sucursal_ids"),
                "tiene_seleccion": False,
                "preview": None,
            }
            if empresa is None:
                return Response(empty)
            if not variante_id and not producto_id:
                return Response(empty)

            try:
                preview_payload = RFIDLabelService.onboarding_preview(
                    request.user,
                    variante_id=variante_id,
                    producto_id=producto_id,
                    cantidad=cantidad,
                    rfid_mode=rfid_mode,
                )
            except ValidationError:
                return Response(
                    {
                        **empty,
                        "error": "Solicitud inválida. Verifica los datos enviados.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return Response({
                "q": q,
                "resultados": buscar_payload.get("resultados", []),
                "sucursal_ids": buscar_payload.get("sucursal_ids"),
                "tiene_seleccion": True,
                "preview": preview_payload,
                "mensaje": (
                    "Next.js: iterar preview.zpl_individual[] y enviar cada ZPL "
                    "a Browser Print. Al terminar hacer POST a este mismo "
                    "endpoint con los campos de impresora/estatus/etiquetas."
                ),
            })

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        impresion = RFIDLabelService.store_impresion(
            serializer.validated_data, request.user
        )
        return Response(
            EtiquetaRFIDSerializer(impresion).data, status=status.HTTP_201_CREATED
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="preview",
        url_name="preview",
    )
    def preview(self, request):
        variante_id = request.query_params.get("variante") or request.query_params.get(
            "variante_id"
        )
        producto_id = request.query_params.get("producto") or request.query_params.get(
            "producto_id"
        )
        cantidad_raw = request.query_params.get("cantidad")
        rfid_mode_raw = request.query_params.get("rfid_mode", "true")
        cantidad = int(cantidad_raw) if (cantidad_raw and str(cantidad_raw).isdigit()) else 1
        rfid_mode = str(rfid_mode_raw).lower() not in {"0", "false", "no", "off"}

        payload = RFIDLabelService.onboarding_preview(request.user, variante_id=variante_id, producto_id=producto_id, cantidad=cantidad, rfid_mode=rfid_mode)
        return Response(payload)

    @action(
        detail=False,
        methods=["post"],
        url_path="registrar-impresion",
        url_name="registrar_impresion",
    )
    def registrar_impresion(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        impresion = RFIDLabelService.store_impresion(
            serializer.validated_data, request.user
        )
        return Response(
            EtiquetaRFIDSerializer(impresion).data, status=status.HTTP_201_CREATED
        )

    def create(self, request):
        return self.registrar_impresion(request)

    # ── SCANNER / LECTOR RFID ───────────────────────────────────────────
    # Next.js consume scans/, scans/clear y scanner-stats.
    # scans/receive lo usa SOLO el lector FX, autenticado con el token de su LectorRFID.

    @action(
        detail=False,
        methods=["post"],
        url_path="scans/receive",
        url_name="scans_receive",
        authentication_classes=[],
        permission_classes=[AllowAny],
    )
    def scans_receive(self, request):
        lector = lector_desde_request(request)
        if lector is None:
            return Response(
                {"status": "error", "message": "Lector no autorizado."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return recibir_lecturas(request._request, lector)

    @extend_schema(
        parameters=[
            OpenApiParameter("lector", OpenApiTypes.INT,
                             description="Solo las lecturas de ese lector (id)."),
            OpenApiParameter("solo_match", OpenApiTypes.BOOL,
                             description="`1`: solo lecturas de etiquetas impresas por el ERP."),
            OpenApiParameter("unicos", OpenApiTypes.BOOL,
                             description="`1`: la última lectura de cada EPC."),
            OpenApiParameter("limite", OpenApiTypes.INT,
                             description="Máximo de lecturas (1-200, default 50)."),
            OpenApiParameter("despues_de_id", OpenApiTypes.INT, description=(
                "Cursor (EC-982): solo lecturas con `id` mayor. Para ignorar lo leído antes de "
                "empezar una captura, manda el `id` más alto que ya viste. Con cursor el orden es "
                "por `id` ascendente (lo más viejo primero, EC-984) y `limite` toma las primeras: "
                "avanzar el cursor al `id` más alto de cada respuesta recorre las lecturas aunque "
                "lleguen más de `limite` entre dos pollings. Puede perder lecturas que se "
                "confirman fuera de orden de `id` (dos `scans/receive/` simultáneos) o que se "
                "ligan después a su empresa o etiqueta (`ligar_lecturas_previas`). Sin cursor, "
                "lo más nuevo primero. Con `unicos`, la última lectura (por `id`) de cada EPC "
                "entre los lectores visibles, solo si es posterior al cursor. Entero >= 0; "
                "inválido → 400. "
                "La respuesta trae `cursor` (EC-998), el `id` con el que pedir lo siguiente: sin "
                "`despues_de_id`, el más alto de todas las lecturas visibles (con el filtro de "
                "`lector`, sin `solo_match` ni `unicos`; 0 si no hay ninguna), para arrancar una "
                "captura sin depender de lo que traiga `scans`; con `despues_de_id`, el más alto "
                "devuelto o, si no vino nada, el mismo `despues_de_id`."
            )),
            OpenApiParameter("epc", OpenApiTypes.STR,
                             description="Debug: busca este EPC entre las lecturas devueltas."),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    @action(
        detail=False,
        methods=["get"],
        url_path="scans",
        url_name="scans",
    )
    def scans(self, request):
        """Polling endpoint p/ Next.js: últimas 50 lecturas del FX + match automático.

        Filtros opcionales: ``lector=<id>`` (solo ese lector),
        ``solo_match=1`` (solo etiquetas impresas por el ERP),
        ``unicos=1`` (última lectura por EPC), ``limite`` (1-200, default 50) y
        ``despues_de_id`` (EC-982: solo lecturas con ``id`` mayor; inválido → 400;
        con él, orden por ``id`` ascendente, EC-984).

        Acotado a la empresa activa del usuario, superusuario incluido (EC-884,
        ``RfidScan.objects.visibles_para``); no admin: lectores de sus sucursales.

        Query params (debug opcional, sin auth):
            epc (opcional): si enviamos un EPC (hex), se busca directamente y en
            debug_get.query_epc_search devuelve found_in_scans true/false + variant.

        Response shape:
            {
              cursor: int (EC-998, ver ``despues_de_id``),
              scans: [
                {id, epc, timestamp, antenna, rssi, reader_ip,
                 match_impresion: bool,
                 impresion_folio, impresion_id, producto_nombre, sku, color, talla,
                 barcode_value, serial, estado, detalle_id,
                 match_debug: {scan_epc, detalle_epc_raw | null}
                }, ...
              ],
              debug_get: {...lookup info, query_epc_search si lo mandaste...}
            }
        """
        user = request.user
        params = request.query_params
        base = RfidScan.objects.visibles_para(user)
        # ``solo_match=1``: solo lecturas de etiquetas impresas por el ERP; los tags
        # ajenos cerca del lector llenaban las 50 en un minuto.
        solo_match = str(params.get("solo_match", "")).lower() in {"1", "true", "si"}
        if solo_match:
            base = base.filter(etiqueta__isnull=False)
        # ``despues_de_id`` (EC-982): solo lecturas con ``id`` mayor. Con ``unicos``
        # sale la última lectura de cada EPC (``RfidLecturaUltima``) solo si es
        # posterior al cursor; ``limite`` se aplica después.
        despues_de_id = entero_param(request, "despues_de_id")
        if despues_de_id is not None:
            base = base.filter(pk__gt=despues_de_id)
        # ``lector=<id>``: solo las lecturas de ese lector (combinable con los
        # otros dos). Sin él, todas las de la empresa, como antes.
        lector_param = str(params.get("lector", "")).strip()
        lector_id = None
        if lector_param:
            if not lector_param.isdigit():
                raise ValidationError({"lector": "Debe ser el id numérico de un lector."})
            lector_id = int(lector_param)
            base = base.filter(lector_id=lector_id)
        try:
            limite = min(max(int(params.get("limite", 50)), 1), 200)
        except (TypeError, ValueError):
            limite = 50
        # ``unicos=1``: la última lectura de cada EPC, desde ``RfidLecturaUltima``
        # (una fila por lector y EPC): el costo depende de cuántos tags distintos
        # hay, no de cuántas lecturas acumula la tabla.
        if str(params.get("unicos", "")).lower() in {"1", "true", "si"}:
            ultimas = RfidLecturaUltima.objects.filter(empresa_id=getattr(user, "empresa_id", None))
            if lector_id is not None:
                ultimas = ultimas.filter(lector_id=lector_id)
            else:
                # Solo lectores visibles ANTES del ``distinct`` (EC-984): si no, la
                # última de un lector de otra sucursal tapaba la del propio y el EPC
                # no salía para un no admin.
                ultimas = ultimas.filter(lector_id__in=lectores_visibles(user).values("pk"))
                if despues_de_id is not None:
                    # Con cursor, la última por ``id`` (el mismo eje que el cursor),
                    # no por ``visto``: una lectura posterior al cursor nunca la tapa
                    # otra de ``id`` menor.
                    ultimas = ultimas.filter(scan_id__gt=despues_de_id).order_by("epc", "-scan_id")
                else:
                    ultimas = ultimas.order_by("epc", "-visto")
                ultimas = ultimas.distinct("epc")
            if despues_de_id is not None:
                # Con cursor, el recorte va dentro (EC-984): las primeras ``limite``
                # por ``id`` posteriores al cursor (``solo_match`` incluido), en una
                # consulta aparte (dos fijas por polling). Como subconsulta con
                # LIMIT, Postgres no la convierte en semi-join y recorre todas las
                # lecturas de la empresa cuando el cursor es bajo.
                ultimas = RfidLecturaUltima.objects.filter(
                    pk__in=ultimas.values("pk"), scan_id__gt=despues_de_id
                )
                if solo_match:
                    ultimas = ultimas.filter(scan__etiqueta__isnull=False)
                base = base.filter(
                    pk__in=list(ultimas.order_by("scan_id").values_list("scan_id", flat=True)[:limite])
                )
            else:
                base = base.filter(pk__in=ultimas.values("scan_id"))
        # ``cursor`` sin ``despues_de_id`` (EC-998), ANTES de leer ``scans``: una
        # lectura que se confirme entre las dos consultas queda arriba del cursor y
        # sale en el siguiente polling; calculado después, quedaba debajo sin haber
        # salido nunca.
        cursor = self._cursor_de_arranque(user, lector_id) if despues_de_id is None else None
        # El match ya viene ligado desde que se recibió la lectura (``etiqueta``):
        # una sola consulta, sin recalcular variantes de EPC en cada polling.
        scans = list(
            base
            .select_related(
                "lector",
                "etiqueta__impresion__producto",
                "etiqueta__impresion__producto_variante__color",
                "etiqueta__impresion__producto_variante__talla",
            )
            # Con cursor (EC-984), por ``id`` ascendente: si llegan más de ``limite``
            # entre dos pollings, salen las más viejas y el siguiente cursor (el
            # ``id`` más alto devuelto) recoge el resto sin saltarse ninguna.
            .order_by(*(("id",) if despues_de_id is not None else ("-created_at", "-id")))[:limite]
        )
        # ``lector`` de otra empresa o de una sucursal ajena → 404, como el detalle
        # del lector. Solo se consulta si no hubo lecturas: con lecturas, el
        # lector ya es visible (mismo alcance que ``visibles_para``).
        if lector_id is not None and not scans:
            if not lectores_visibles(user).filter(pk=lector_id).exists():
                raise NotFound({"lector": "No existe ese lector."})
        # Con ``despues_de_id``, el ``id`` más alto devuelto, sin consulta extra.
        if despues_de_id is not None:
            cursor = max((s.pk for s in scans), default=despues_de_id)
        # Mismo alcance que get_queryset: un no admin solo ve el detalle de las
        # etiquetas impresas en sus sucursales.
        sucursales = None
        if not (getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)):
            if any(s.etiqueta_id for s in scans):
                sucursales = set(user.sucursales_permitidas())

        data = []
        ligadas = 0
        for scan in scans:
            epc = scan.epc or ""
            detalle = scan.etiqueta
            impresion = detalle.impresion if detalle is not None else None
            if impresion is not None and sucursales is not None and impresion.sucursal_id not in sucursales:
                detalle = impresion = None
            item = {
                "id": scan.pk,
                "epc": epc,
                "timestamp": scan.created_at.isoformat(),
                "antenna": scan.antenna,
                "rssi": scan.rssi,
                "reader_ip": scan.reader_ip,
                "lector_id": scan.lector_id,
                "lector_nombre": scan.lector.nombre if scan.lector_id else None,
            }
            if detalle is not None:
                ligadas += 1
                variante = impresion.producto_variante
                producto = impresion.producto
                nombre_producto = sku = color_nombre = talla_nombre = None
                if variante:
                    sku = variante.sku
                    if variante.color:
                        color_nombre = variante.color.nombre
                    if variante.talla:
                        talla_nombre = variante.talla.nombre
                    nombre_producto = variante.nombre or (producto.nombre if producto else None)
                elif producto:
                    nombre_producto = producto.nombre
                item.update({
                    "match_impresion": True,
                    "impresion_folio": impresion.folio,
                    "impresion_id": impresion.id,
                    "producto_nombre": nombre_producto,
                    "sku": sku,
                    "color": color_nombre,
                    "talla": talla_nombre,
                    "barcode_value": detalle.barcode_value,
                    "serial": detalle.serial,
                    "estado": detalle.estado,
                    "detalle_id": detalle.id,
                    "match_debug": {"scan_epc": epc, "detalle_epc_raw": detalle.epc},
                })
            else:
                item["match_impresion"] = False
                item["match_debug"] = {"scan_epc": epc, "detalle_epc_raw": None}
            data.append(item)

        epcs = {(s.epc or "").upper() for s in scans if s.epc}
        q_epc = normalizar_epc(request.query_params.get("epc"))
        q_search_debug = None
        if q_epc:
            hit = next((v for v in sorted(variantes_epc(q_epc)) if v in epcs), None)
            q_search_debug = {
                "query_epc": q_epc,
                "found_in_scans": bool(hit),
                "hit_variant": hit,
            }
        debug_get = {
            "scans_returned": len(data),
            "scans_ligados": ligadas,
            "unique_epc_in_50_scans_count": len(epcs),
            "unique_epc_prefixes_head30": sorted({e[:4] for e in epcs})[:30],
            "query_epc_search": q_search_debug,
        }
        return Response({"scans": data, "debug_get": debug_get, "cursor": cursor})

    @staticmethod
    def _cursor_de_arranque(user, lector_id):
        """``cursor`` de ``scans/`` sin ``despues_de_id`` (EC-998): el ``id`` más alto
        de todas las lecturas visibles (``visibles_para``, con ``lector`` pero sin
        ``solo_match`` ni ``unicos``), 0 si no hay. No el de ``scans``: con el feed
        vacío o filtrado, arrancar en 0 o abajo dejaba entrar lecturas viejas que se
        ligan después a una etiqueta (``ligar_lecturas_previas``). ``ORDER BY id DESC
        LIMIT 1`` y no ``MAX(id)``: con el filtro de sucursal de un no admin el MAX
        recorría todas las lecturas de la empresa."""
        visibles = RfidScan.objects.visibles_para(user)
        if lector_id is not None:
            visibles = visibles.filter(lector_id=lector_id)
        return visibles.order_by("-id").values_list("id", flat=True).first() or 0

    @action(
        detail=False,
        methods=["get"],
        url_path="scanner-stats",
        url_name="scanner_stats",
    )
    def scanner_stats(self, request):
        """Endpoint 1-clic para saber si el lector FX esta vivo SIN entrar a Vercel.

        Solo superusuario o administrador de empresa. Acotado a la empresa activa
        del usuario, superusuario incluido (EC-884); sin empresa, todo en cero.

        Query params:
            epc (opcional): buscar si un EPC (ej recién impreso) existe en RfidScan.

        Response shape:
            {
              total_rfidscan_rows, last_scan_ts, last_scan_seconds_ago,
              last_5_scans: [{id,epc,epc_len,antenna,rssi,reader_ip,ts}, ...],
              query_epc_found_count, query_epc_found_samples,
              receive_endpoint_info: {fx_post_url_required, example_POST_test_1_tag}
            }
        """
        user = request.user
        if not (getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)):
            raise PermissionDenied("No tiene permisos para realizar esta acción.")

        scans_qs = RfidScan.objects.visibles_para(user)
        # Conteo acotado: no recorre toda la tabla en cada llamada. Arriba de
        # ``TOPE_CONTEO_STATS`` reporta el tope y ``total_es_tope: true``.
        total = scans_qs[:TOPE_CONTEO_STATS + 1].count()
        total_es_tope = total > TOPE_CONTEO_STATS
        total = min(total, TOPE_CONTEO_STATS)
        last_5 = list(
            scans_qs.order_by("-created_at", "-id")[:5].values(
                "id", "epc", "antenna", "rssi", "reader_ip", "created_at"
            )
        )
        last_5_serializable = []
        for s in last_5:
            last_5_serializable.append({
                "id": s["id"],
                "epc": s["epc"],
                "epc_len": len(s["epc"] or ""),
                "antenna": s["antenna"],
                "rssi": s["rssi"],
                "reader_ip": s["reader_ip"],
                "ts": s["created_at"].isoformat() if s["created_at"] else None,
            })
        last_scan_ts = last_5_serializable[0]["ts"] if last_5_serializable else None
        last_scan_how_old_secs = None
        if last_scan_ts:
            try:
                from django.utils import timezone as dj_tz
                dt = dj_tz.datetime.fromisoformat(last_scan_ts.replace("Z", "+00:00"))
                last_scan_how_old_secs = int((dj_tz.now() - dt).total_seconds())
            except Exception:
                pass

        q_epc = (request.query_params.get("epc") or "").strip().lower()
        q_found_samples = []
        if q_epc:
            def _v(epc):
                base = epc
                vars = {base, base.lstrip("0"), base.rstrip("0"), base.strip("0")}
                if len(base) > 24:
                    vars |= {base[:24], base[-24:]}
                if len(base) < 24:
                    vars |= {base.rjust(24, "0"), base.ljust(24, "0")}
                return vars
            base_vars = _v(q_epc)
            q_lookup = list(base_vars) + [v.upper() for v in base_vars]
            qs_found = scans_qs.filter(epc__in=q_lookup).order_by("-created_at")[:10]
            for f in qs_found:
                q_found_samples.append({
                    "id": f.id, "epc": f.epc, "epc_len": len(f.epc or ""),
                    "antenna": f.antenna, "rssi": f.rssi,
                    "ts": f.created_at.isoformat() if f.created_at else None,
                })

        payload = {
            "status": "ok",
            "lectores": lectores_estado(user),
            "total_rfidscan_rows": total,
            "total_es_tope": total_es_tope,
            "last_scan_ts": last_scan_ts,
            "last_scan_seconds_ago": last_scan_how_old_secs,
            "last_5_scans": last_5_serializable,
            "query_epc": q_epc or None,
            "query_epc_found_count": len(q_found_samples),
            "query_epc_found_samples": q_found_samples,
            "receive_endpoint_info": {
                "fx_post_url_required": "POST https://TU-BACKEND/api/v1/wms/etiquetas-rfid/scans/receive/ (el FX llama aquí, NO Next.js)",
                "method_required": (
                    "POST con token del LectorRFID: Basic (usuario libre, contraseña = token; "
                    "así lo configura el IoT Connector del FX), header X-RFID-Token o ?token=. "
                    "Next.js NO usa receive/"
                ),
                "example_POST_test_1_tag": (
                    "Invoke-RestMethod -Uri 'https://nucleo-erp.vercel.app/api/v1/wms/etiquetas-rfid/scans/receive/' "
                    "-Method POST -Headers @{'X-RFID-Token'='<token>'} -ContentType 'application/json' -Body "
                    "ConvertTo-Json(@(@{epcId='000012e32827000147c0c5f5';antennaPort=1;peakRssiValue=-45}))"
                ),
                "note": "Next.js solo consume scans/ (polling), scans/clear (purge) y scanner-stats (debug).",
            },
        }
        return Response(payload)

    @action(
        detail=False,
        methods=["post"],
        url_path="scans/clear",
        url_name="scans_clear",
    )
    def scans_clear(self, request):
        """Purge list: borra las lecturas de la empresa activa del usuario
        (opcional: ``lector`` y/o ``hasta_id``, #1455).
        Respuesta: {"status": "success", "deleted": N}

        Solo superusuario o administrador de empresa. El superusuario también
        borra sólo las de su empresa activa (EC-884); sin empresa, ``deleted: 0``.
        Las lecturas sin empresa no se borran aquí. Un ``DELETE`` filtrado, más el
        de sus ``RfidLecturaUltima`` en cascada (#1460); ``deleted`` cuenta solo
        lecturas (#1458).
        """
        user = request.user
        if not (getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)):
            raise PermissionDenied("No tiene permisos para realizar esta acción.")

        # #1455: acotable. ``lector`` = solo ese lector; ``hasta_id`` = solo las
        # lecturas con id <= N (lo leído antes de empezar una captura). Sin filtros
        # sigue siendo el mantenimiento de toda la empresa.
        qs = RfidScan.objects.visibles_para(user)
        datos = request.data if hasattr(request.data, "get") else {}
        filtros = {}
        for campo, lookup in (("lector", "lector_id"), ("hasta_id", "pk__lte")):
            valor = datos.get(campo, request.query_params.get(campo))
            if valor in (None, ""):
                continue
            try:
                filtros[lookup] = int(valor)
            except (TypeError, ValueError):
                raise ValidationError({campo: "Debe ser un número entero."})
        _, por_modelo = qs.filter(**filtros).delete()
        deleted = por_modelo.get(RfidScan._meta.label, 0)
        return Response({"status": "success", "deleted": deleted, "filtros": {
            "lector": filtros.get("lector_id"), "hasta_id": filtros.get("pk__lte")}})
