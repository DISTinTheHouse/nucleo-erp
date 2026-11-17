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
from django.db.models import Sum
from django.utils import timezone
from rest_framework import mixins, serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from nucleo.models import SerieFolio
from produccion.models import (
    OrdenBordadoDetalle, OrdenesBordado, OrdenProduccion, OrdenProduccionDetalle, SalidaMaquila, SalidaMaquilaDetalle,
)
from produccion.scope import ordenes_bordado_visibles, ordenes_produccion_visibles
from terceros.models import Proveedor


class SalidaMaquilaDetalleSerializer(serializers.ModelSerializer):
    descripcion = serializers.CharField(max_length=255, required=False, allow_blank=True)
    # > 0 (#1088): una maquila sin precio no es válida.
    precio_unitario = serializers.DecimalField(
        max_digits=14, decimal_places=4, min_value=Decimal("0.0001"), required=False,
        error_messages={"min_value": "El precio de maquila debe ser mayor que 0."},
    )
    cantidad = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))

    class Meta:
        model = SalidaMaquilaDetalle
        fields = ["id", "op_detalle", "ob_detalle", "descripcion", "cantidad", "precio_unitario", "importe"]
        read_only_fields = ["id", "importe"]
        extra_kwargs = {"op_detalle": {"required": False, "allow_null": True,
                                       "queryset": OrdenProduccionDetalle.objects.none()},
                        "ob_detalle": {"required": False, "allow_null": True,
                                       "queryset": OrdenBordadoDetalle.objects.none()}}


class SalidaMaquilaSerializer(serializers.ModelSerializer):
    detalles = SalidaMaquilaDetalleSerializer(many=True, required=False)
    op_folio = serializers.CharField(source="op.folio_op", read_only=True, allow_null=True, default=None)
    ob_folio = serializers.CharField(source="ob.folio_bordado", read_only=True, allow_null=True, default=None)
    # Documento de origen, para mostrar de dónde viene cada SM.
    origen = serializers.SerializerMethodField()
    origen_id = serializers.SerializerMethodField()
    origen_folio = serializers.SerializerMethodField()

    def get_origen(self, obj):
        return "OB" if obj.ob_id else "OP"

    def get_origen_id(self, obj):
        return obj.ob_id or obj.op_id

    def get_origen_folio(self, obj):
        return obj.ob.folio_bordado if obj.ob_id else obj.op.folio_op
    centro_confeccion_nombre = serializers.CharField(source="centro_confeccion.nombre", read_only=True)
    estatus_display = serializers.CharField(source="get_estatus_display", read_only=True)
    fecha_envio = serializers.DateField(required=False)

    class Meta:
        model = SalidaMaquila
        fields = [
            "id", "folio", "estatus", "estatus_display", "origen", "origen_id", "origen_folio",
            "op", "op_folio", "ob", "ob_folio",
            "centro_confeccion", "centro_confeccion_nombre", "sucursal",
            "fecha_envio", "fecha_compromiso", "fecha_recepcion",
            "total_piezas", "total", "observaciones", "usuario", "created_at", "detalles",
        ]
        read_only_fields = ["folio", "estatus", "sucursal", "fecha_recepcion", "total_piezas", "total",
                            "usuario", "created_at"]
        extra_kwargs = {"centro_confeccion": {"required": False}, "op": {"required": False},
                        "ob": {"required": False}}

    def get_fields(self):
        fields = super().get_fields()
        user = getattr(self.context.get("request"), "user", None)
        empresa_id = getattr(user, "empresa_id", None)
        # EC-871: solo OP de las sucursales del usuario (mismo alcance que el listado de OP).
        fields["op"].queryset = ordenes_produccion_visibles(OrdenProduccion.objects.filter(activo=True), user)
        fields["ob"].queryset = ordenes_bordado_visibles(OrdenesBordado.objects.filter(activo=True), user)
        fields["centro_confeccion"].queryset = Proveedor.objects.filter(empresa_id=empresa_id)
        if "detalles" in fields:
            hijos = fields["detalles"].child.fields
            hijos["op_detalle"].queryset = OrdenProduccionDetalle.objects.filter(op__empresa_id=empresa_id)
            hijos["ob_detalle"].queryset = OrdenBordadoDetalle.objects.filter(ob__empresa_id=empresa_id)
        return fields

    def validate(self, attrs):
        op, ob = attrs.get("op"), attrs.get("ob")
        if (op is None) == (ob is None):
            raise ValidationError({"op": "Indica la OP o la OB de origen (solo una)."})
        if op is not None and op.estatus_op == OrdenProduccion.EstatusOrdenProduccion.CANCELADO:
            raise ValidationError({"op": "La OP está cancelada."})
        if ob is not None and ob.estatus_bordado == OrdenesBordado.EstatusBordado.CANCELADO_LEGACY:
            raise ValidationError({"ob": "La OB está cancelada."})
        maquilero = attrs.get("centro_confeccion") or (op.centro_confeccion if op else ob.proveedor)
        if maquilero is None:
            sigla = "OP" if op else "OB"
            raise ValidationError({"centro_confeccion": f"Indica el centro de confección (la {sigla} no tiene)."})
        if not (maquilero.activo and maquilero.es_maquilero):
            raise ValidationError({"centro_confeccion": "El proveedor no está activo como maquilero."})
        attrs["centro_confeccion"] = maquilero
        envio = attrs.get("fecha_envio") or timezone.localdate()
        compromiso = attrs.get("fecha_compromiso")
        if compromiso and compromiso < envio:
            raise ValidationError({"fecha_compromiso": "No puede ser anterior a la fecha de envío."})
        for d in attrs.get("detalles", []):
            if d.get("op_detalle") is not None and (op is None or d["op_detalle"].op_id != op.pk):
                raise ValidationError({"detalles": "Un renglón no pertenece a la OP."})
            if d.get("ob_detalle") is not None and (ob is None or d["ob_detalle"].ob_id != ob.pk):
                raise ValidationError({"detalles": "Un renglón no pertenece a la OB."})
        return attrs


class SalidaMaquilaListSerializer(SalidaMaquilaSerializer):
    """Mismo renglón sin ``detalles``: serializarlos y descartarlos costaba una
    consulta por salida."""

    class Meta(SalidaMaquilaSerializer.Meta):
        fields = [f for f in SalidaMaquilaSerializer.Meta.fields if f != "detalles"]


TIPOS_SERIE_SM = ["Salida a Maquila", "SALIDA_MAQUILA", "SM"]


def _serie_sm(empresa_id, sucursal_id):
    """Serie SM de la sucursal (#1091: una por sucursal). Si la sucursal no
    tiene, se da de alta al primer uso con el formato de la serie SM existente
    de la empresa; el prefijo lleva el id de la sucursal (``SM61``) porque el
    folio es único por empresa. Mismo patrón que la serie de pólizas."""
    if SerieFolio.resolve(empresa_id, sucursal_id, TIPOS_SERIE_SM, lock=True) is not None:
        return
    molde = (
        SerieFolio.objects.filter(empresa_id=empresa_id, tipo_documento__in=TIPOS_SERIE_SM)
        .order_by("id_serie_folio").first()
    )
    serie, creada = SerieFolio.objects.get_or_create(
        sucursal_id=sucursal_id, tipo_documento="Salida a Maquila", serie=f"SM{sucursal_id}",
        defaults={
            "empresa_id": empresa_id, "folio_actual": 0,
            "relleno_ceros": molde.relleno_ceros if molde else 5,
            "separador": molde.separador if molde else "-",
        },
    )
    if not creada and not serie.activo:
        serie.activo = True
        serie.save(update_fields=["activo", "updated_at"])


def _descripcion(det):
    v = det.producto_variante or det.variante_produccion
    if v is None:
        return f"Renglón {det.pk}"
    return getattr(v, "sku", None) or str(v)


def _descripcion_ob(det):
    partes = [getattr(det.producto, "nombre", None), getattr(det.talla, "nombre", None),
              getattr(det.color, "nombre", None)]
    return " ".join(p for p in partes if p) or f"Renglón OB {det.pk}"


def _exigir_produccion(user):
    """Mismo gate que el resto de producción, pero sin permiso responde 403
    (EC-871), no el 400 ``{"permiso": ...}`` de ``_require_produccion``."""
    from produccion.api.views import _require_produccion
    try:
        _require_produccion(user)
    except ValidationError as exc:
        raise PermissionDenied(exc.detail.get("permiso") if isinstance(exc.detail, dict) else None)


class SalidaMaquilaViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                           GenericViewSet):
    serializer_class = SalidaMaquilaSerializer

    def get_queryset(self):
        # EC-871: empresa activa + sucursales del usuario (la SM lleva la sucursal
        # de su OP), el mismo alcance que la OP: lo que no ve en OP da 404 aquí.
        qs = (
            ordenes_produccion_visibles(SalidaMaquila.objects.filter(activo=True), self.request.user)
            .select_related("op", "ob", "centro_confeccion")
            .order_by("-id")
        )
        if self.action == "retrieve":
            qs = qs.prefetch_related("detalles")
        p = self.request.query_params
        for param, campo in (("op", "op_id"), ("ob", "ob_id"), ("centro_confeccion", "centro_confeccion_id")):
            valor = p.get(param)
            if valor:
                if not str(valor).isdigit():
                    raise ValidationError({param: "Debe ser un número entero."})
                qs = qs.filter(**{campo: int(valor)})
        origen = (p.get("origen") or "").upper()
        if origen:
            if origen not in ("OP", "OB"):
                raise ValidationError({"origen": "Opciones: OP, OB."})
            qs = qs.filter(**{f"{origen.lower()}__isnull": False})
        if p.get("estatus"):
            if p["estatus"] not in SalidaMaquila.Estatus.values:
                raise ValidationError({"estatus": f"Opciones: {', '.join(SalidaMaquila.Estatus.values)}."})
            qs = qs.filter(estatus=p["estatus"])
        return qs

    def list(self, request, *args, **kwargs):
        # El listado no lleva renglones (sin consultas por fila).
        return Response(
            SalidaMaquilaListSerializer(self.get_queryset(), many=True, context=self.get_serializer_context()).data
        )

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        _exigir_produccion(user)
        datos = serializer.validated_data
        renglones = datos.pop("detalles", None)
        # Bloquea el documento de origen: dos SM simultáneas no rebasan juntas el tope (#1088).
        if datos.get("op") is not None:
            doc = OrdenProduccion.objects.select_for_update().get(pk=datos["op"].pk)
            sigla, precio_op = "OP", doc.costo_maquila_unitario or Decimal("0")
            base = doc.orden_produccion_detalle.filter(activo=True)
            if not renglones:
                renglones = [
                    {"op_detalle": d, "cantidad": d.cantidad, "precio_unitario": precio_op}
                    for d in base.select_related("producto_variante", "variante_produccion")
                ]
            programado = base.aggregate(t=Sum("cantidad"))["t"] or Decimal("0")
            enviadas = SalidaMaquilaDetalle.objects.filter(salida__op=doc)
        else:
            # OB: sus renglones; la OB no tiene costo de maquila (precio por renglón).
            doc = OrdenesBordado.objects.select_for_update().get(pk=datos["ob"].pk)
            sigla, precio_op = "OB", Decimal("0")
            base = list(doc.detalles.select_related("producto", "talla", "color"))
            if not renglones:
                renglones = [{"ob_detalle": d, "cantidad": Decimal(str(d.cantidad))} for d in base]
            programado = sum((Decimal(str(d.cantidad)) for d in base), Decimal("0"))
            enviadas = SalidaMaquilaDetalle.objects.filter(salida__ob=doc)
        if not renglones:
            raise ValidationError({"detalles": f"La {sigla} no tiene renglones; captura los de la salida."})
        if any((precio_op if r.get("precio_unitario") is None else r["precio_unitario"]) <= 0 for r in renglones):
            raise ValidationError({"precio_unitario": (
                f"La {sigla} no tiene costo de maquila: indica el precio_unitario de cada renglón (mayor que 0)."
            )})
        # Tope total por documento (#1088): Σ piezas de sus SM no canceladas + esta
        # ≤ Σ piezas de sus renglones.
        enviado = (
            enviadas.filter(salida__activo=True).exclude(salida__estatus=SalidaMaquila.Estatus.CANCELADA)
            .aggregate(t=Sum("cantidad"))["t"] or Decimal("0")
        )
        nuevo = sum((r["cantidad"] for r in renglones), Decimal("0"))
        if enviado + nuevo > programado:
            raise ValidationError({"detalles": (
                f"La salida excede las piezas de la {sigla}: {programado:g} programadas, "
                f"{enviado:g} ya enviadas a maquila; quedan {max(programado - enviado, Decimal('0')):g}."
            )})
        try:
            _serie_sm(doc.empresa_id, doc.sucursal_id)
            folio = SerieFolio.consumir_siguiente_folio(
                doc.empresa_id, doc.sucursal_id, TIPOS_SERIE_SM,
                descripcion_documento="Salida a Maquila",
            )
        except Exception as exc:  # serie no configurada / agotada
            raise ValidationError({"folio": str(getattr(exc, "message", exc))})
        sm = serializer.save(
            empresa_id=doc.empresa_id, sucursal_id=doc.sucursal_id, folio=folio, usuario=user,
            fecha_envio=serializer.validated_data.get("fecha_envio") or timezone.localdate(),
        )
        filas, piezas, total = [], Decimal("0"), Decimal("0")
        for r in renglones:
            precio = r.get("precio_unitario")
            precio = precio_op if precio is None else precio
            importe = (r["cantidad"] * precio).quantize(Decimal("0.01"))
            det, det_ob = r.get("op_detalle"), r.get("ob_detalle")
            filas.append(SalidaMaquilaDetalle(
                salida=sm, op_detalle=det, ob_detalle=det_ob, cantidad=r["cantidad"], precio_unitario=precio,
                importe=importe,
                descripcion=(r.get("descripcion") or (_descripcion(det) if det else "")
                             or (_descripcion_ob(det_ob) if det_ob else "") or "Prenda")[:255],
            ))
            piezas += r["cantidad"]
            total += importe
        SalidaMaquilaDetalle.objects.bulk_create(filas)
        sm.total_piezas, sm.total = piezas, total
        sm.save(update_fields=["total_piezas", "total", "updated_at"])

    def create(self, request, *args, **kwargs):
        _exigir_produccion(request.user)  # 403 antes de validar el cuerpo
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        sm = SalidaMaquila.objects.prefetch_related("detalles").select_related("op", "ob", "centro_confeccion").get(
            pk=serializer.instance.pk
        )
        return Response(SalidaMaquilaSerializer(sm, context=self.get_serializer_context()).data,
                        status=status.HTTP_201_CREATED)

    def _cambiar(self, request, nuevo, permitido_desde):
        _exigir_produccion(request.user)
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
