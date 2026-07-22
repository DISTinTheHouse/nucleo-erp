from django.db.models import Prefetch
from rest_framework.response import Response
from rest_framework import mixins, status
from rest_framework.viewsets import GenericViewSet
from wms.api.serializers import (
    TransferenciaListSerializer,
    TransferenciaRetrieveSerializer,
    TransferenciaSerializer,
    PickingSerializer
)
from wms.models import Transferencia, TransferenciaDetalle
from wms.services.transferencia_service import TransferenciaService
from wms.models import Picking, PickingDetalle
from wms.services.picking_service import PickingService

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
        # sucursales; el resto solo las asignadas en el M2M ``usuario.sucursales``
        # —mismo criterio que ``MovimientoInventarioViewSet`` en inventarios, que
        # scope por ``sucursal_id__in`` el mismo tipo de dato—. Sin sucursales
        # asignadas no ve nada, igual que un usuario sin empresa: se falla cerrado.
        # Se filtra por ``Transferencia.sucursal`` (la dueña del documento), no por
        # la sucursal de los almacenes origen/destino, que pueden diferir.
        if getattr(user, "is_admin_empresa", False):
            return qs
        sucursal_ids = list(user.sucursales.values_list("pk", flat=True))
        return qs.filter(sucursal_id__in=sucursal_ids)

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
                        "ubicacion",
                        "ubicacion__almacen",
                        "operador",
                    ).order_by("id"),
                )
            )
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
        # sucursales; el resto solo las asignadas en el M2M ``usuario.sucursales``
        # —mismo criterio que ``TransferenciaViewSet.get_queryset()``—. Sin
        # sucursales asignadas no ve nada, igual que un usuario sin empresa: se
        # falla cerrado. Se filtra por ``Picking.sucursal`` (la dueña del
        # documento), no por la sucursal del almacén, que puede diferir.
        if getattr(user, "is_admin_empresa", False):
            return qs
        sucursal_ids = list(user.sucursales.values_list("pk", flat=True))
        return qs.filter(sucursal_id__in=sucursal_ids)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        res = PickingService.handle_store(serializer.validated_data, request.user)
        return Response(PickingSerializer(res).data, status=status.HTTP_201_CREATED)
