"""SM — salida de prendas a maquila: ``/api/v1/produccion/salidas-maquila/``.

- ``GET`` listado (``?op=``, ``?centro_confeccion=``, ``?estatus=``) y detalle.
- ``POST`` crea con folio SM. Body: un solo origen, ``op``, ``ob`` u ``orden_r``;
  ``centro_confeccion`` (si no, el de la OP o el proveedor de la OB; la OR no
  tiene), ``fecha_envio`` (hoy), ``fecha_compromiso``, ``observaciones`` y
  ``detalles`` ``[{op_detalle?|ob_detalle?|or_detalle?, descripcion?, cantidad,
  precio_unitario?}]``. OP sin ``detalles``: se arman de sus renglones con el
  ``costo_maquila_unitario`` como precio. OB/OR no tienen costo de maquila:
  ``detalles`` con ``precio_unitario`` es obligatorio (#1218).
- Errores 400 siempre ``{campo: ["mensaje"]}`` (#1222).
- Lectura: ``R-PRODUCCION-OP``; ``total``, ``precio_unitario`` e ``importe`` solo
  con ``R-PRODUCCION-COSTOS`` (#1211).
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
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from nucleo.models import SerieFolio
from nucleo.permisos import CODIGOS_PRODUCCION_OP, LecturaPorCodigo, permisos_de_la_peticion
from produccion.models import (
    OrdenBordadoDetalle, OrdenesBordado, OrdenesReflejante, OrdenReflejanteDetalle, OrdenProduccion, OrdenProduccionDetalle, SalidaMaquila, SalidaMaquilaDetalle,
)
from produccion.scope import ordenes_bordado_visibles, ordenes_produccion_visibles, ordenes_reflejante_visibles
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
        fields = ["id", "op_detalle", "ob_detalle", "or_detalle", "descripcion", "cantidad", "precio_unitario", "importe"]
        read_only_fields = ["id", "importe"]
        extra_kwargs = {"op_detalle": {"required": False, "allow_null": True,
                                       "queryset": OrdenProduccionDetalle.objects.none()},
                        "ob_detalle": {"required": False, "allow_null": True,
                                       "queryset": OrdenBordadoDetalle.objects.none()},
                        "or_detalle": {"required": False, "allow_null": True,
                                       "queryset": OrdenReflejanteDetalle.objects.none()}}


class SalidaMaquilaSerializer(serializers.ModelSerializer):
    detalles = SalidaMaquilaDetalleSerializer(many=True, required=False)
    op_folio = serializers.CharField(source="op.folio_op", read_only=True, allow_null=True, default=None)
    ob_folio = serializers.CharField(source="ob.folio_bordado", read_only=True, allow_null=True, default=None)
    orden_r_folio = serializers.CharField(source="orden_r.folio_reflejante", read_only=True, allow_null=True,
                                          default=None)
    # Documento de origen, para mostrar de dónde viene cada SM.
    origen = serializers.SerializerMethodField()
    origen_id = serializers.SerializerMethodField()
    origen_folio = serializers.SerializerMethodField()
    # Para pantalla: "SM-00012 · OR-00006" (el folio no cambia).
    folio_display = serializers.SerializerMethodField()

    def get_folio_display(self, obj):
        return f"{obj.folio} · {self.get_origen_folio(obj)}"

    def get_origen(self, obj):
        return "OB" if obj.ob_id else "OR" if obj.orden_r_id else "OP"

    def get_origen_id(self, obj):
        return obj.ob_id or obj.orden_r_id or obj.op_id

    def get_origen_folio(self, obj):
        if obj.ob_id:
            return obj.ob.folio_bordado
        if obj.orden_r_id:
            return obj.orden_r.folio_reflejante
        return obj.op.folio_op
    centro_confeccion_nombre = serializers.CharField(source="centro_confeccion.nombre", read_only=True)
    sucursal_nombre = serializers.CharField(source="sucursal.nombre", read_only=True, default=None)
    usuario_nombre = serializers.SerializerMethodField()

    def get_usuario_nombre(self, obj):
        u = obj.usuario
        if u is None:
            return None
        return u.get_full_name().strip() or u.email

    def _ve_costos(self):
        """``R-PRODUCCION-COSTOS`` (#1211, como el costo de maquila de la OP, #1135)."""
        request = self.context.get("request")
        if request is None or not getattr(request, "user", None):
            return False
        from produccion.api.serializers import PERMISO_COSTOS_PRODUCCION
        return PERMISO_COSTOS_PRODUCCION in permisos_de_la_peticion(request)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self._ve_costos():
            data.pop("total", None)
            for d in data.get("detalles") or []:
                d.pop("precio_unitario", None)
                d.pop("importe", None)
        return data
    estatus_display = serializers.CharField(source="get_estatus_display", read_only=True)
    fecha_envio = serializers.DateField(required=False)

    class Meta:
        model = SalidaMaquila
        fields = [
            "id", "folio", "folio_display", "estatus", "estatus_display", "origen", "origen_id", "origen_folio",
            "op", "op_folio", "ob", "ob_folio", "orden_r", "orden_r_folio",
            "centro_confeccion", "centro_confeccion_nombre", "sucursal", "sucursal_nombre",
            "fecha_envio", "fecha_compromiso", "fecha_recepcion",
            "total_piezas", "total", "observaciones", "usuario", "usuario_nombre", "created_at", "detalles",
        ]
        read_only_fields = ["folio", "estatus", "sucursal", "fecha_recepcion", "total_piezas", "total",
                            "usuario", "created_at"]
        extra_kwargs = {"centro_confeccion": {"required": False}, "op": {"required": False},
                        "ob": {"required": False}, "orden_r": {"required": False}}

    def get_fields(self):
        fields = super().get_fields()
        user = getattr(self.context.get("request"), "user", None)
        empresa_id = getattr(user, "empresa_id", None)
        # EC-871: solo OP de las sucursales del usuario (mismo alcance que el listado de OP).
        fields["op"].queryset = ordenes_produccion_visibles(OrdenProduccion.objects.filter(activo=True), user)
        fields["ob"].queryset = ordenes_bordado_visibles(OrdenesBordado.objects.filter(activo=True), user)
        fields["orden_r"].queryset = ordenes_reflejante_visibles(OrdenesReflejante.objects.filter(activo=True), user)
        fields["centro_confeccion"].queryset = Proveedor.objects.filter(empresa_id=empresa_id)
        if "detalles" in fields:
            hijos = fields["detalles"].child.fields
            hijos["op_detalle"].queryset = OrdenProduccionDetalle.objects.filter(op__empresa_id=empresa_id)
            hijos["ob_detalle"].queryset = OrdenBordadoDetalle.objects.filter(ob__empresa_id=empresa_id)
            hijos["or_detalle"].queryset = OrdenReflejanteDetalle.objects.filter(orden_r__empresa_id=empresa_id)
        return fields

    def validate(self, attrs):
        op, ob, orr = attrs.get("op"), attrs.get("ob"), attrs.get("orden_r")
        enviados = [k for k, v in (("op", op), ("ob", ob), ("orden_r", orr)) if v is not None]
        if len(enviados) != 1:
            # La llave es la del primer origen enviado (o ``op`` si no llegó ninguno).
            raise ValidationError({(enviados or ["op"])[0]: "Indica el documento de origen: OP, OB u OR (solo uno)."})
        if op is not None and op.estatus_op == OrdenProduccion.EstatusOrdenProduccion.CANCELADO:
            raise ValidationError({"op": "La OP está cancelada."})
        if ob is not None and ob.estatus_bordado == OrdenesBordado.EstatusBordado.CANCELADO_LEGACY:
            raise ValidationError({"ob": "La OB está cancelada."})
        if orr is not None and orr.estatus_reflejante == OrdenesReflejante.EstatusReflejante.CANCELADO:
            raise ValidationError({"orden_r": "La OR está cancelada."})
        # La OR no tiene maquilero propio: se indica en la salida.
        maquilero = attrs.get("centro_confeccion") or (
            op.centro_confeccion if op else ob.proveedor if ob else None)
        if maquilero is None:
            sigla = "OP" if op else "OB" if ob else "OR"
            raise ValidationError({"centro_confeccion": f"Indica el centro de confección (la {sigla} no tiene)."})
        if not (maquilero.activo and maquilero.es_maquilero):
            raise ValidationError({"centro_confeccion": "El proveedor no está activo como maquilero."})
        attrs["centro_confeccion"] = maquilero
        envio = attrs.get("fecha_envio") or timezone.localdate()
        compromiso = attrs.get("fecha_compromiso")
        if compromiso and compromiso < envio:
            raise ValidationError({"fecha_compromiso": "No puede ser anterior a la fecha de envío."})
        sigla, doc = ("OP", op) if op else ("OB", ob) if ob else ("OR", orr)
        propio = {"OP": "op_detalle", "OB": "ob_detalle", "OR": "or_detalle"}[sigla]
        fk_doc = {"OP": "op_id", "OB": "ob_id", "OR": "orden_r_id"}[sigla]
        detalles = attrs.get("detalles") or []
        for d in detalles:
            # #1221: renglón de otro tipo que el origen ≠ renglón de otro documento.
            if any(d.get(c) is not None for c in ("op_detalle", "ob_detalle", "or_detalle") if c != propio):
                raise ValidationError({"detalles": f"La salida es de la {sigla}: sus renglones van con {propio}."})
            if d.get(propio) is not None and getattr(d[propio], fk_doc) != doc.pk:
                raise ValidationError({"detalles": f"Un renglón no pertenece a la {sigla}."})
        if sigla != "OP":
            # #1218: OB/OR no tienen costo de maquila; se captura por renglón.
            if not detalles:
                raise ValidationError({"detalles": (
                    f"Para una salida desde {sigla} captura los renglones con su precio_unitario.")})
            if any(d.get("precio_unitario") is None for d in detalles):
                raise ValidationError({"precio_unitario": (
                    f"La {sigla} no tiene costo de maquila: indica el precio_unitario de cada renglón (mayor que 0).")})
        return attrs


class SalidaMaquilaListSerializer(SalidaMaquilaSerializer):
    """Mismo renglón sin ``detalles``: serializarlos y descartarlos costaba una
    consulta por salida."""

    class Meta(SalidaMaquilaSerializer.Meta):
        fields = [f for f in SalidaMaquilaSerializer.Meta.fields if f != "detalles"]


TIPOS_SERIE_SM = ["Salida a Maquila", "SALIDA_MAQUILA", "SM"]
_RELACIONES = ("op", "ob", "orden_r", "centro_confeccion", "sucursal", "usuario")


def _n(valor):
    """Piezas sin ceros de sobra: ``20``, ``15``, ``5.5`` (#1222)."""
    return f"{Decimal(str(valor)).normalize():f}"


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
    return " ".join(p for p in partes if p) or f"Renglón {det.pk}"


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
    permission_classes = [IsAuthenticated, LecturaPorCodigo]
    codigos_lectura = {"list": CODIGOS_PRODUCCION_OP, "retrieve": CODIGOS_PRODUCCION_OP}

    def get_queryset(self):
        # EC-871: empresa activa + sucursales del usuario (la SM lleva la sucursal
        # de su OP), el mismo alcance que la OP: lo que no ve en OP da 404 aquí.
        qs = (
            ordenes_produccion_visibles(SalidaMaquila.objects.filter(activo=True), self.request.user)
            .select_related(*_RELACIONES)
            .order_by("-id")
        )
        if self.action == "retrieve":
            qs = qs.prefetch_related("detalles")
        p = self.request.query_params
        for param, campo in (("op", "op_id"), ("ob", "ob_id"), ("orden_r", "orden_r_id"),
                             ("centro_confeccion", "centro_confeccion_id")):
            valor = p.get(param)
            if valor:
                if not str(valor).isdigit():
                    raise ValidationError({param: "Debe ser un número entero."})
                qs = qs.filter(**{campo: int(valor)})
        origen = (p.get("origen") or "").upper()
        if origen:
            if origen not in ("OP", "OB", "OR"):
                raise ValidationError({"origen": "Opciones: OP, OB, OR."})
            campo = {"OP": "op", "OB": "ob", "OR": "orden_r"}[origen]
            qs = qs.filter(**{f"{campo}__isnull": False})
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
            # OB / OR: sus renglones; no tienen costo de maquila (precio por renglón).
            if datos.get("ob") is not None:
                doc, sigla, campo_det = OrdenesBordado.objects.select_for_update().get(pk=datos["ob"].pk), "OB", "ob_detalle"
                enviadas = SalidaMaquilaDetalle.objects.filter(salida__ob=doc)
            else:
                doc, sigla, campo_det = (OrdenesReflejante.objects.select_for_update().get(pk=datos["orden_r"].pk),
                                         "OR", "or_detalle")
                enviadas = SalidaMaquilaDetalle.objects.filter(salida__orden_r=doc)
            precio_op = Decimal("0")
            base = list(doc.detalles.select_related("producto", "talla", "color"))
            if not renglones:
                renglones = [{campo_det: d, "cantidad": Decimal(str(d.cantidad))} for d in base]
            programado = sum((Decimal(str(d.cantidad)) for d in base), Decimal("0"))
        if not renglones:
            raise ValidationError({"detalles": [f"La {sigla} no tiene renglones; captura los de la salida."]})
        if any((precio_op if r.get("precio_unitario") is None else r["precio_unitario"]) <= 0 for r in renglones):
            raise ValidationError({"precio_unitario": [
                f"La {sigla} no tiene costo de maquila: indica el precio_unitario de cada renglón (mayor que 0)."
            ]})
        # Tope total por documento (#1088): Σ piezas de sus SM no canceladas + esta
        # ≤ Σ piezas de sus renglones.
        enviado = (
            enviadas.filter(salida__activo=True).exclude(salida__estatus=SalidaMaquila.Estatus.CANCELADA)
            .aggregate(t=Sum("cantidad"))["t"] or Decimal("0")
        )
        nuevo = sum((r["cantidad"] for r in renglones), Decimal("0"))
        if enviado + nuevo > programado:
            raise ValidationError({"detalles": [
                f"La salida excede las piezas de la {sigla}: {_n(programado)} programadas, "
                f"{_n(enviado)} ya enviadas a maquila; quedan {_n(max(programado - enviado, Decimal('0')))}."
            ]})
        try:
            _serie_sm(doc.empresa_id, doc.sucursal_id)
            folio = SerieFolio.consumir_siguiente_folio(
                doc.empresa_id, doc.sucursal_id, TIPOS_SERIE_SM,
                descripcion_documento="Salida a Maquila",
            )
        except Exception as exc:  # serie no configurada / agotada
            raise ValidationError({"folio": [str(getattr(exc, "message", exc))]})
        sm = serializer.save(
            empresa_id=doc.empresa_id, sucursal_id=doc.sucursal_id, folio=folio, usuario=user,
            fecha_envio=serializer.validated_data.get("fecha_envio") or timezone.localdate(),
        )
        filas, piezas, total = [], Decimal("0"), Decimal("0")
        for r in renglones:
            precio = r.get("precio_unitario")
            precio = precio_op if precio is None else precio
            importe = (r["cantidad"] * precio).quantize(Decimal("0.01"))
            det, det_ob, det_or = r.get("op_detalle"), r.get("ob_detalle"), r.get("or_detalle")
            det_servicio = det_ob or det_or
            filas.append(SalidaMaquilaDetalle(
                salida=sm, op_detalle=det, ob_detalle=det_ob, or_detalle=det_or, cantidad=r["cantidad"],
                precio_unitario=precio, importe=importe,
                descripcion=(r.get("descripcion") or (_descripcion(det) if det else "")
                             or (_descripcion_ob(det_servicio) if det_servicio else "") or "Prenda")[:255],
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
        sm = SalidaMaquila.objects.prefetch_related("detalles").select_related(*_RELACIONES).get(
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
        """Ya no se cierra a mano: la SM queda Recibida al recepcionarla en WMS
        (``compras/recepciones/onboarding/`` con ``salida_maquila``), que además
        registra la recepción y crea la factura de maquila para CxP.
        Antes: ``self._cambiar(request, SalidaMaquila.Estatus.RECIBIDA, {SalidaMaquila.Estatus.ENVIADA})``."""
        self.get_object()  # 404 fuera de alcance, como antes
        raise ValidationError({"estatus": "La SM se recibe en WMS (Recepciones → Orden de Producción)."})

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        return self._cambiar(request, SalidaMaquila.Estatus.CANCELADA, {SalidaMaquila.Estatus.ENVIADA})
