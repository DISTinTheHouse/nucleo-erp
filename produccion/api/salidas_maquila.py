"""SM — salida de prendas a maquila: ``/api/v1/produccion/salidas-maquila/``.

- ``GET`` listado (``?op=``, ``?centro_confeccion=``, ``?estatus=``) y detalle.
- ``POST`` crea con folio SM. Body: ``op`` (obligatorio), ``centro_confeccion``
  (si no, el de la OP), ``fecha_envio`` (hoy), ``fecha_compromiso``,
  ``observaciones`` y ``detalles`` ``[{op_detalle?, descripcion?, cantidad,
  precio_unitario?}]``. Sin ``detalles`` se arman de los renglones de la OP con
  el ``costo_maquila_unitario`` de la OP como precio.
- ``POST {id}/cerrar/``: WMS recibió y pasó calidad → ``RECIBIDA``.
- ``POST {id}/cancelar/``.

No mueve inventario (la OP ya consumió la materia prima). Siempre la empresa
activa, superusuario incluido (decisión #361 de producción). Escrituras: solo
producción, superusuario o admin de empresa.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from nucleo.models import SerieFolio
from produccion.models import OrdenProduccion, OrdenProduccionDetalle, SalidaMaquila, SalidaMaquilaDetalle
from terceros.models import Proveedor


class SalidaMaquilaDetalleSerializer(serializers.ModelSerializer):
    descripcion = serializers.CharField(max_length=255, required=False, allow_blank=True)
    precio_unitario = serializers.DecimalField(max_digits=14, decimal_places=4, min_value=0, required=False)
    cantidad = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))

    class Meta:
        model = SalidaMaquilaDetalle
        fields = ["id", "op_detalle", "descripcion", "cantidad", "precio_unitario", "importe"]
        read_only_fields = ["id", "importe"]
        extra_kwargs = {"op_detalle": {"required": False, "allow_null": True,
                                       "queryset": OrdenProduccionDetalle.objects.none()}}


class SalidaMaquilaSerializer(serializers.ModelSerializer):
    detalles = SalidaMaquilaDetalleSerializer(many=True, required=False)
    op_folio = serializers.CharField(source="op.folio_op", read_only=True)
    centro_confeccion_nombre = serializers.CharField(source="centro_confeccion.nombre", read_only=True)
    estatus_display = serializers.CharField(source="get_estatus_display", read_only=True)
    fecha_envio = serializers.DateField(required=False)

    class Meta:
        model = SalidaMaquila
        fields = [
            "id", "folio", "estatus", "estatus_display", "op", "op_folio",
            "centro_confeccion", "centro_confeccion_nombre", "sucursal",
            "fecha_envio", "fecha_compromiso", "fecha_recepcion",
            "total_piezas", "total", "observaciones", "usuario", "created_at", "detalles",
        ]
        read_only_fields = ["folio", "estatus", "sucursal", "fecha_recepcion", "total_piezas", "total",
                            "usuario", "created_at"]
        extra_kwargs = {"centro_confeccion": {"required": False}}

    def get_fields(self):
        fields = super().get_fields()
        empresa_id = getattr(getattr(self.context.get("request"), "user", None), "empresa_id", None)
        fields["op"].queryset = OrdenProduccion.objects.filter(empresa_id=empresa_id, activo=True)
        fields["centro_confeccion"].queryset = Proveedor.objects.filter(empresa_id=empresa_id)
        fields["detalles"].child.fields["op_detalle"].queryset = OrdenProduccionDetalle.objects.filter(
            op__empresa_id=empresa_id
        )
        return fields

    def validate(self, attrs):
        op = attrs["op"]
        if op.estatus_op == OrdenProduccion.EstatusOrdenProduccion.CANCELADO:
            raise ValidationError({"op": "La OP está cancelada."})
        maquilero = attrs.get("centro_confeccion") or op.centro_confeccion
        if maquilero is None:
            raise ValidationError({"centro_confeccion": "Indica el centro de confección (la OP no tiene)."})
        if not (maquilero.activo and maquilero.es_maquilero):
            raise ValidationError({"centro_confeccion": "El proveedor no está activo como maquilero."})
        attrs["centro_confeccion"] = maquilero
        for d in attrs.get("detalles", []):
            if d.get("op_detalle") is not None and d["op_detalle"].op_id != op.pk:
                raise ValidationError({"detalles": "Un renglón no pertenece a la OP."})
        return attrs


def _descripcion(det):
    v = det.producto_variante or det.variante_produccion
    if v is None:
        return f"Renglón {det.pk}"
    return getattr(v, "sku", None) or str(v)


class SalidaMaquilaViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                           GenericViewSet):
    serializer_class = SalidaMaquilaSerializer

    def get_queryset(self):
        empresa_id = getattr(self.request.user, "empresa_id", None)
        if not empresa_id:
            return SalidaMaquila.objects.none()
        qs = (
            SalidaMaquila.objects.filter(empresa_id=empresa_id, activo=True)
            .select_related("op", "centro_confeccion")
            .order_by("-id")
        )
        if self.action == "retrieve":
            qs = qs.prefetch_related("detalles")
        p = self.request.query_params
        for param, campo in (("op", "op_id"), ("centro_confeccion", "centro_confeccion_id")):
            valor = p.get(param)
            if valor:
                if not str(valor).isdigit():
                    raise ValidationError({param: "Debe ser un número entero."})
                qs = qs.filter(**{campo: int(valor)})
        if p.get("estatus"):
            if p["estatus"] not in SalidaMaquila.Estatus.values:
                raise ValidationError({"estatus": f"Opciones: {', '.join(SalidaMaquila.Estatus.values)}."})
            qs = qs.filter(estatus=p["estatus"])
        return qs

    def list(self, request, *args, **kwargs):
        # El listado no lleva renglones (sin consultas por fila).
        data = SalidaMaquilaSerializer(self.get_queryset(), many=True, context=self.get_serializer_context()).data
        for fila in data:
            fila.pop("detalles", None)
        return Response(data)

    @transaction.atomic
    def perform_create(self, serializer):
        from produccion.api.views import _require_produccion

        user = self.request.user
        _require_produccion(user)
        op = serializer.validated_data["op"]
        renglones = serializer.validated_data.pop("detalles", None)
        precio_op = op.costo_maquila_unitario or Decimal("0")
        if not renglones:
            renglones = [
                {"op_detalle": d, "cantidad": d.cantidad, "precio_unitario": precio_op}
                for d in op.orden_produccion_detalle.filter(activo=True)
                .select_related("producto_variante", "variante_produccion")
            ]
            if not renglones:
                raise ValidationError({"detalles": "La OP no tiene renglones; captura los de la salida."})
        try:
            folio = SerieFolio.consumir_siguiente_folio(
                op.empresa_id, op.sucursal_id, ["Salida a Maquila", "SALIDA_MAQUILA", "SM"],
                descripcion_documento="Salida a Maquila",
            )
        except Exception as exc:  # serie no configurada / agotada
            raise ValidationError({"folio": str(getattr(exc, "message", exc))})
        sm = serializer.save(
            empresa_id=op.empresa_id, sucursal_id=op.sucursal_id, folio=folio, usuario=user,
            fecha_envio=serializer.validated_data.get("fecha_envio") or timezone.localdate(),
        )
        filas, piezas, total = [], Decimal("0"), Decimal("0")
        for r in renglones:
            precio = r.get("precio_unitario")
            precio = precio_op if precio is None else precio
            importe = (r["cantidad"] * precio).quantize(Decimal("0.01"))
            det = r.get("op_detalle")
            filas.append(SalidaMaquilaDetalle(
                salida=sm, op_detalle=det, cantidad=r["cantidad"], precio_unitario=precio, importe=importe,
                descripcion=(r.get("descripcion") or (_descripcion(det) if det else "") or "Prenda")[:255],
            ))
            piezas += r["cantidad"]
            total += importe
        SalidaMaquilaDetalle.objects.bulk_create(filas)
        sm.total_piezas, sm.total = piezas, total
        sm.save(update_fields=["total_piezas", "total", "updated_at"])

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        sm = SalidaMaquila.objects.prefetch_related("detalles").select_related("op", "centro_confeccion").get(
            pk=serializer.instance.pk
        )
        return Response(SalidaMaquilaSerializer(sm, context=self.get_serializer_context()).data,
                        status=status.HTTP_201_CREATED)

    def _cambiar(self, request, nuevo, permitido_desde):
        from produccion.api.views import _require_produccion

        _require_produccion(request.user)
        with transaction.atomic():
            sm = self.get_queryset().select_for_update(of=("self",)).get(pk=self.get_object().pk)
            if sm.estatus not in permitido_desde:
                raise ValidationError({"estatus": f"No se puede pasar de {sm.get_estatus_display()} a {nuevo.label}."})
            sm.estatus = nuevo
            campos = ["estatus", "updated_at"]
            if nuevo == SalidaMaquila.Estatus.RECIBIDA:
                sm.fecha_recepcion = timezone.now()
                campos.append("fecha_recepcion")
            sm.save(update_fields=campos)
        return Response(SalidaMaquilaSerializer(sm, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"], url_path="cerrar")
    def cerrar(self, request, pk=None):
        """WMS recibió la producción y pasó calidad."""
        return self._cambiar(request, SalidaMaquila.Estatus.RECIBIDA, {SalidaMaquila.Estatus.ENVIADA})

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        return self._cambiar(request, SalidaMaquila.Estatus.CANCELADA, {SalidaMaquila.Estatus.ENVIADA})
