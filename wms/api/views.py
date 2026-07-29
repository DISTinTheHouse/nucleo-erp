from django.db.models import Prefetch
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from wms.api.serializers import (
    DespachoCreateSerializer,
    DespachoSerializer,
    PackingCreateSerializer,
    TransferenciaListSerializer,
    TransferenciaRetrieveSerializer,
    TransferenciaSerializer,
    PickingCreateSerializer,
    PickingSerializer,
    PackingSerializer,
)
from wms.models import (
    Despacho,
    DespachoDetalle,
    Packing,
    PackingDetalle,
    Picking,
    PickingDetalle,
    PickingOrdenTrabajo,
    Transferencia,
    TransferenciaDetalle,
)
from wms.services.despacho_service import DespachoService
from wms.services.transferencia_service import TransferenciaService
from wms.services.picking_service import PickingService
from wms.services.packing_service import PackingService


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
            .select_related("almacen_origen", "almacen_destino", "usuario")
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
                        "ubicacion_origen",
                        "ubicacion_origen__almacen",
                        "ubicacion_destino",
                        "ubicacion_destino__almacen",
                    ).order_by("id"),
                )
            )

        # Aislamiento multi-tenant: sin empresa no se ve nada. list devuelve 200 []
        # fuera de alcance; retrieve de otra empresa/sucursal devuelve 404 (no 403).
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)

        # Scope por sucursal dentro de la empresa: el admin de empresa ve todas sus
        # sucursales; el resto ve las asignadas en el M2M ``usuario.sucursales``
        # más su ``sucursal_default`` —mismo criterio que
        # ``PickingViewSet``/``PackingViewSet``/``DespachoViewSet.get_queryset()``,
        # y que ``TransferenciaService.handle_store()`` ya exige del lado de
        # escritura (la transferencia se timbra con ``user.sucursal_default``)—.
        # Sin esto un usuario cuyo único acceso fuera la sucursal por defecto podía
        # crear la transferencia pero no volver a verla en list/retrieve. Sin
        # sucursales asignadas no ve nada, igual que un usuario sin empresa: se
        # falla cerrado. Se filtra por ``Transferencia.sucursal`` (la dueña del
        # documento), no por la sucursal de los almacenes origen/destino, que
        # pueden diferir.
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

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
        res = TransferenciaService.handle_store(serializer.validated_data, request.user)
        return Response(TransferenciaSerializer(res).data, status=status.HTTP_201_CREATED)

class PickingViewSet(mixins.RetrieveModelMixin, mixins.ListModelMixin, GenericViewSet):
    queryset = Picking.objects.all()
    serializer_class = PickingSerializer

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
                "operador",
                "almacen",
                "almacen_destino",
                "usuario",
                "oleada",
                "zona_almacen",
                "lote",
            )
            .prefetch_related(
                Prefetch(
                    "picking_detalle",
                    queryset=PickingDetalle.objects.select_related(
                        "producto",
                        "producto_variante",
                        "pedido_detalle_talla",
                        "pedido_detalle_talla__variante",
                        "pedido_detalle_talla__variante__talla",
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

        # Aislamiento multi-tenant: sin empresa no se ve nada. list devuelve 200 []
        # fuera de alcance; retrieve de otra empresa/sucursal devuelve 404 (no 403).
        # Mismo criterio que TransferenciaViewSet.get_queryset().
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)

        # Scope por sucursal dentro de la empresa: el admin de empresa ve todas sus
        # sucursales; el resto ve las asignadas en el M2M ``usuario.sucursales``
        # más su ``sucursal_default`` —mismo criterio que
        # ``PackingViewSet``/``DespachoViewSet.get_queryset()``—. Sin esto un
        # usuario cuyo único acceso fuera la sucursal por defecto podía completar
        # el onboarding (que sí une ambos conjuntos, ver
        # ``PickingService.onboarding_payload``) y crear el picking, pero no
        # volver a verlo en list/retrieve. Sin sucursales asignadas no ve nada,
        # igual que un usuario sin empresa: se falla cerrado. Se filtra por
        # ``Picking.sucursal`` (la dueña del documento), no por la sucursal del
        # almacén, que puede diferir.
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

    def get_serializer_class(self):
        if self.action in {"create", "onboarding"} and self.request.method == "POST":
            return PickingCreateSerializer
        return PickingSerializer

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
            )
            return Response(payload)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        picking, ordenes_trabajo = PickingService.handle_store(serializer.validated_data, request.user)
        response_data = PickingSerializer(picking).data
        response_data["ordenes_trabajo_generadas"] = ordenes_trabajo
        return Response(response_data, status=status.HTTP_201_CREATED)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        picking, ordenes_trabajo = PickingService.handle_store(serializer.validated_data, request.user)
        response_data = PickingSerializer(picking).data
        response_data["ordenes_trabajo_generadas"] = ordenes_trabajo
        return Response(response_data, status=status.HTTP_201_CREATED)

class PackingViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    queryset = Packing.objects.all()
    serializer_class = PackingSerializer

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related("pedido", "picking", "picking__almacen", "operador", "usuario")
            .prefetch_related(
                Prefetch(
                    "packing_detalle",
                    queryset=PackingDetalle.objects.select_related(
                        "caja",
                        "picking_detalle",
                        "picking_detalle__producto",
                        "picking_detalle__producto_variante",
                        "picking_detalle__pedido_detalle_talla",
                        "picking_detalle__pedido_detalle_talla__variante",
                        "picking_detalle__pedido_detalle_talla__variante__talla",
                        "picking_detalle__ubicacion",
                        "picking_detalle__ubicacion__almacen",
                    ).order_by("id"),
                )
            )
            .order_by("-created_at", "-id")
        )

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
            return qs
        return qs.filter(sucursal_id__in=user.sucursales_permitidas())

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
        return Response(PackingSerializer(packing_instance).data, status=status.HTTP_201_CREATED)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        packing_instance = PackingService.handle_store(serializer.validated_data, request.user)
        return Response(PackingSerializer(packing_instance).data, status=status.HTTP_201_CREATED)


class DespachoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
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
            .order_by("-id")
        )

        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(packing__empresa=empresa)
        if getattr(user, "is_admin_empresa", False):
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
        return Response(DespachoSerializer(despacho_instance).data, status=status.HTTP_201_CREATED)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        despacho_instance = DespachoService.handle_store(serializer.validated_data, request.user)
        return Response(DespachoSerializer(despacho_instance).data, status=status.HTTP_201_CREATED)
