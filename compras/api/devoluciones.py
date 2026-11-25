"""``/api/v1/compras/devoluciones-proveedor/``: devolución a proveedor de lo que
calidad rechazó o puso en cuarentena. Lógica en ``compras.services.devolucion_proveedor``."""
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from compras.api.serializers import _nombre_usuario
from compras.models import CalidadInspeccion, DevolucionProveedor, DevolucionProveedorDetalle
from compras.services import devolucion_proveedor as servicio
from nucleo.kpis import entero_param
from nucleo.listados import OrdenamientoEstableFilter, PaginacionOpcionalLimitOffset
from nucleo.middleware import ip_auditoria

PERMISO_LEER = "R-WMS-RECEPCIONES"
PERMISO_ESCRIBIR = "C-WMS-RECEPCIONES"


class DevolucionProveedorDetalleSerializer(serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source="producto.nombre", read_only=True, default=None)

    class Meta:
        model = DevolucionProveedorDetalle
        fields = ["id", "calidad_detalle", "recepcion_detalle", "producto", "producto_nombre", "producto_variante",
                  "variante_produccion", "cantidad", "costo_unitario", "importe"]


class DevolucionProveedorSerializer(serializers.ModelSerializer):
    detalles = DevolucionProveedorDetalleSerializer(many=True, read_only=True)
    proveedor_nombre = serializers.CharField(source="proveedor.razon_social", read_only=True, default=None)
    recepcion_folio = serializers.CharField(source="recepcion.folio", read_only=True, default=None)
    orden_compra_folio = serializers.CharField(source="orden_compra.folio", read_only=True, default=None)
    usuario_nombre = serializers.CharField(source="usuario.get_full_name", read_only=True, default=None)
    # Nombre completo o correo; ``null`` sin cancelar (EC-966, como ``_CancelacionRecepcionMixin``).
    # ``cancelado_por`` va en el ``select_related`` del viewset.
    cancelado_por_nombre = serializers.SerializerMethodField()

    class Meta:
        model = DevolucionProveedor
        fields = ["id", "folio", "estatus", "calidad_inspeccion", "recepcion", "recepcion_folio", "orden_compra",
                  "orden_compra_folio", "proveedor", "proveedor_nombre", "almacen", "motivo", "fecha", "usuario",
                  "usuario_nombre", "total", "movimiento_inventario", "motivo_cancelacion", "fecha_cancelacion",
                  "cancelado_por", "cancelado_por_nombre", "detalles"]

    def get_cancelado_por_nombre(self, obj):
        return _nombre_usuario(obj.cancelado_por) if obj.cancelado_por_id else None


def _motivo(request, mensaje):
    motivo = request.data.get("motivo") if hasattr(request.data, "get") else None
    if not isinstance(motivo, str) or not motivo.strip():
        raise ValidationError({"motivo": mensaje})
    return motivo.strip()[:500]


class DevolucionProveedorViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DevolucionProveedorSerializer
    http_method_names = ["get", "post"]
    # EC-973: como OC y recepciones. Sin ``?limit=`` sigue la lista completa; con él,
    # ``{count, next, previous, results}``. ``?search=`` solo en el listado.
    pagination_class = PaginacionOpcionalLimitOffset
    search_fields = [
        "folio", "recepcion__folio", "orden_compra__folio", "proveedor__nombre", "proveedor__razon_social",
    ]
    filter_backends = [
        *[b for b in viewsets.ReadOnlyModelViewSet.filter_backends if b is not OrderingFilter],
        OrdenamientoEstableFilter,
    ]
    # Lista blanca de ``?ordering=`` (#863); sin él, ``-fecha, -id``.
    ordering_fields = [
        "id", "folio", "fecha", "estatus", "total", "proveedor__nombre", "recepcion__folio", "orden_compra__folio",
    ]

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return DevolucionProveedor.objects.none()
        qs = (
            DevolucionProveedor.objects.filter(empresa=empresa)
            .select_related("proveedor", "recepcion", "orden_compra", "usuario", "cancelado_por")
            .prefetch_related("detalles__producto")
            .order_by("-fecha", "-id")
        )
        # Ids no enteros → 400 como los demás listados (#1402); antes, 500.
        for campo in ("recepcion", "calidad_inspeccion", "orden_compra", "proveedor"):
            valor = entero_param(self.request, campo)
            if valor is not None:
                qs = qs.filter(**{f"{campo}_id": valor})
        if self.request.query_params.get("estatus"):
            qs = qs.filter(estatus=self.request.query_params["estatus"])
        return qs

    def _exigir(self, clave):
        if not self.request.user.tiene_permiso(clave):
            raise PermissionDenied(f"No tienes permiso para esta acción ({clave}).")

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method == "GET":
            self._exigir(PERMISO_LEER)

    def create(self, request, *args, **kwargs):
        """``{"calidad_inspeccion", "motivo", "detalles": [{"calidad_detalle", "cantidad"}]?}``."""
        self._exigir(PERMISO_ESCRIBIR)
        motivo = _motivo(request, "Indica el motivo de la devolución.")
        empresa = getattr(request.user, "empresa", None)
        try:
            pk = int(request.data.get("calidad_inspeccion") or 0)
        except (TypeError, ValueError):
            pk = 0
        inspeccion = CalidadInspeccion(pk=pk) if empresa and pk and CalidadInspeccion.objects.filter(
            pk=pk, recepcion__empresa=empresa).exists() else None
        if inspeccion is None:
            raise ValidationError({"calidad_inspeccion": "Inspección no encontrada."})
        detalles = request.data.get("detalles") or []
        if not isinstance(detalles, list):
            raise ValidationError({"detalles": "Debe ser una lista."})
        dev = servicio.crear(
            inspeccion=inspeccion, motivo=motivo, renglones=detalles, user=request.user,
            ip=ip_auditoria(request), user_agent=request.META.get("HTTP_USER_AGENT"),
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=dev.pk)).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        self._exigir(PERMISO_ESCRIBIR)
        motivo = _motivo(request, "Indica el motivo de la cancelación.")
        dev = self.get_object()  # otra empresa: 404
        servicio.cancelar(
            devolucion=dev, motivo=motivo, user=request.user,
            ip=ip_auditoria(request), user_agent=request.META.get("HTTP_USER_AGENT"),
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=dev.pk)).data)

    @action(detail=False, methods=["get"], url_path="pendientes")
    def pendientes(self, request):
        """``?calidad_inspeccion=`` → renglones rechazo/cuarentena con lo que falta devolver
        y su artículo (nombre, SKU, talla, color; EC-973). Inspección anulada → ``[]``
        (el POST la rechaza); sin los renglones ya devueltos completos. 2 consultas sin
        importar los renglones."""
        empresa = getattr(request.user, "empresa", None)
        pk = entero_param(request, "calidad_inspeccion")  # no entero → 400 (#1402)
        if not empresa or not pk or not CalidadInspeccion.objects.filter(pk=pk, recepcion__empresa=empresa).exists():
            raise ValidationError({"calidad_inspeccion": "Inspección no encontrada."})
        filas = []
        for k, (f, v) in servicio.pendientes(pk).items():
            if v <= 0:  # ya devuelto completo: el POST no lo acepta
                continue
            rd = f.recepcion_detalle
            filas.append({
                "calidad_detalle": k, "recepcion_detalle": rd.pk, "resultado": f.resultado,
                "producto": rd.producto_id, "producto_nombre": rd.articulo_nombre,
                # Mismos nombres que los renglones de la recepción y de la OC.
                "producto_variante_sku": rd.articulo_sku, "producto_variante_talla": rd.articulo_talla,
                "producto_variante_color": rd.articulo_color, "cantidad_pendiente": str(v),
            })
        return Response(filas)
