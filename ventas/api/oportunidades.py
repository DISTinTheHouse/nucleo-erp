"""Oportunidades: lo que pasa antes de la cotización.

Cotizada y Ganada no se guardan, se calculan de las cotizaciones ligadas.
"""

from decimal import Decimal

from django.db.models import Case, CharField, Count, Exists, F, Min, OuterRef, Q, Subquery, Sum, Value, When
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response

from terceros.cartera import cartera_de_cliente
from usuarios.models import Usuario
from ventas.models import Cotizacion, Oportunidad, OportunidadSeguimiento, Pedido
from ventas.scope import oportunidades_base, oportunidades_visibles

ESTATUS_BORRADOR = 1
ESTATUS_AUTORIZADA = 3
ESTADOS = ["Nueva", "Contactada", "Cotizada", "Ganada", "Perdida"]


def con_estado(qs):
    """Anota ``avance`` (Cotizada/Ganada) y ``estado`` con dos EXISTS, sin una consulta por fila."""
    cotizaciones = Cotizacion.objects.filter(oportunidad=OuterRef("pk"))
    autorizadas = cotizaciones.filter(estatus=ESTATUS_AUTORIZADA)
    qs = qs.annotate(
        avance=Case(
            When(Exists(autorizadas), then=Value("Ganada")),
            When(Exists(cotizaciones), then=Value("Cotizada")),
            default=Value(""),
            output_field=CharField(),
        )
    )
    return qs.annotate(
        estado=Case(
            When(etapa=Oportunidad.Etapa.PERDIDA, then=Value("Perdida")),
            When(~Q(avance=""), then=F("avance")),
            default=F("etapa"),
            output_field=CharField(),
        )
    )


class OportunidadSerializer(serializers.ModelSerializer):
    cliente_nombre = serializers.CharField(source="cliente.nombre", read_only=True, default=None)
    vendedor_nombre = serializers.CharField(source="vendedor.get_full_name", read_only=True, default=None)
    estado = serializers.SerializerMethodField()

    class Meta:
        model = Oportunidad
        fields = [
            "id", "cliente", "cliente_nombre", "vendedor", "vendedor_nombre", "nombre_contacto",
            "telefono", "correo", "titulo", "monto_estimado", "etapa", "estado", "motivo_perdida",
            "proximo_contacto", "notas", "created_at", "updated_at",
        ]
        read_only_fields = ["vendedor", "created_at", "updated_at"]

    def get_estado(self, obj):
        if obj.etapa == Oportunidad.Etapa.PERDIDA:
            return "Perdida"
        return getattr(obj, "avance", "") or obj.etapa

    def validate_cliente(self, cliente):
        user = self.context["request"].user
        empresa_id = getattr(getattr(user, "empresa", None), "pk", None)
        if cliente is not None and cliente.empresa_id != empresa_id:
            raise serializers.ValidationError("Cliente no encontrado.")
        return cliente

    def validate(self, attrs):
        etapa = attrs.get("etapa", getattr(self.instance, "etapa", None))
        if etapa == Oportunidad.Etapa.PERDIDA:
            motivo = attrs.get("motivo_perdida", getattr(self.instance, "motivo_perdida", ""))
            if not (motivo or "").strip():
                raise serializers.ValidationError({"motivo_perdida": "Indica por qué se perdió."})
            if self.instance and Cotizacion.objects.filter(
                oportunidad=self.instance, estatus=ESTATUS_AUTORIZADA
            ).exists():
                raise serializers.ValidationError({"etapa": "Una oportunidad ganada no se puede perder."})
        return attrs


class OportunidadViewSet(viewsets.ModelViewSet):
    queryset = oportunidades_base()
    serializer_class = OportunidadSerializer
    http_method_names = ["get", "post", "patch", "delete"]
    pagination_class = LimitOffsetPagination  # sin ?limit= regresa la lista completa

    def get_queryset(self):
        qs = oportunidades_visibles(super().get_queryset(), self.request.user)
        qs = con_estado(qs.select_related("cliente", "vendedor"))
        if self.action in ("list", "embudo"):
            qs = self._filtrar(qs)
        return qs.order_by("-id")

    def _filtrar(self, qs):
        params = self.request.query_params
        estado = params.get("estado")
        if estado:
            if estado not in ESTADOS:
                raise ValidationError({"estado": f"Estado inválido. Usa uno de: {', '.join(ESTADOS)}."})
            qs = qs.filter(estado=estado)
        if params.get("cliente"):
            qs = qs.filter(cliente_id=params["cliente"])
        if params.get("vendedor"):
            qs = qs.filter(vendedor_id=params["vendedor"])
        buscar = (params.get("q") or "").strip()
        if buscar:
            qs = qs.filter(Q(titulo__icontains=buscar) | Q(nombre_contacto__icontains=buscar))
        hasta = params.get("proximo_contacto_hasta")
        if hasta:
            fecha = parse_date(hasta)
            if fecha is None:
                raise ValidationError({"proximo_contacto_hasta": "Fecha inválida (AAAA-MM-DD)."})
            qs = qs.filter(proximo_contacto__lte=fecha).exclude(estado__in=["Ganada", "Perdida"])
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})
        serializer.save(empresa=empresa, vendedor=user)

    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=False, methods=["get"])
    def embudo(self, request):
        """Cantidad y monto por estado, en una sola consulta."""
        filas = {
            f["estado"]: f
            for f in self.get_queryset().order_by().values("estado").annotate(
                cantidad=Count("id"), monto=Sum("monto_estimado")
            )
        }
        estados = [
            {
                "estado": e,
                "cantidad": filas.get(e, {}).get("cantidad", 0),
                "monto": str(Decimal(filas.get(e, {}).get("monto") or 0).quantize(Decimal("0.01"))),
            }
            for e in ESTADOS
        ]
        return Response({
            "estados": estados,
            "total": {
                "cantidad": sum(e["cantidad"] for e in estados),
                "monto": str(sum((Decimal(e["monto"]) for e in estados), Decimal("0.00"))),
            },
        })

    @action(detail=True, methods=["post"])
    def transferir(self, request, pk=None):
        """Pasa la oportunidad a otro vendedor de la misma empresa. Solo admin de empresa."""
        user = request.user
        if not (user.is_superuser or user.is_admin_empresa):
            raise PermissionDenied("Solo el administrador de la empresa puede transferir oportunidades.")
        oportunidad = self.get_object()
        vendedor = Usuario.objects.filter(
            pk=request.data.get("vendedor") or 0, empresa_id=oportunidad.empresa_id, is_active=True
        ).first()
        if vendedor is None:
            raise ValidationError({"vendedor": "Vendedor no encontrado en la empresa."})
        oportunidad.vendedor = vendedor
        oportunidad.save(update_fields=["vendedor", "updated_at"])
        return Response(self.get_serializer(oportunidad).data)

    @action(detail=True, methods=["get"])
    def onboarding(self, request, pk=None):
        """Todo lo de la pantalla de una oportunidad en una llamada: datos, seguimientos,
        cotizaciones (con su pedido) y cartera del cliente."""
        oportunidad = self.get_object()
        seguimientos = OportunidadSeguimiento.objects.filter(oportunidad=oportunidad)
        pendientes = seguimientos.filter(completado=False)
        pedido = Pedido.objects.filter(cotizacion=OuterRef("pk")).order_by("-id")
        cotizaciones = (
            Cotizacion.objects.filter(oportunidad=oportunidad)
            .annotate(pedido_id_=Subquery(pedido.values("id")[:1]), pedido_folio=Subquery(pedido.values("folio")[:1]))
            .order_by("-id")
            .values("id", "estatus", "created_at", "autorizada_at", "pedido_id_", "pedido_folio")
        )
        estatus = dict(Cotizacion.CHOICES_ESTATUS)
        return Response({
            "oportunidad": self.get_serializer(oportunidad).data,
            "seguimientos": SeguimientoSerializer(seguimientos[:50], many=True).data,
            "pendientes": pendientes.count(),
            "proximo_recordatorio": pendientes.aggregate(m=Min("fecha_recordatorio"))["m"],
            "cotizaciones": [
                {
                    "id": c["id"], "estatus": c["estatus"], "estatus_display": estatus.get(c["estatus"]),
                    "created_at": c["created_at"], "autorizada_at": c["autorizada_at"],
                    "pedido_id": c["pedido_id_"], "pedido_folio": c["pedido_folio"],
                }
                for c in cotizaciones
            ],
            "cartera": cartera_de_cliente(oportunidad.cliente) if oportunidad.cliente_id else [],
        })

    @action(detail=True, methods=["post"])
    def cotizar(self, request, pk=None):
        """Crea una cotización en Borrador ligada a la oportunidad. Desde ahí sigue el flujo de siempre."""
        oportunidad = self.get_object()
        if oportunidad.etapa == Oportunidad.Etapa.PERDIDA:
            raise ValidationError({"etapa": "No se cotiza una oportunidad perdida."})
        cotizacion = Cotizacion.objects.create(
            empresa_id=oportunidad.empresa_id,
            vendedor=request.user,
            sucursal=getattr(request.user, "sucursal_default", None),
            cliente_id=oportunidad.cliente_id,
            oportunidad=oportunidad,
            estatus=ESTATUS_BORRADOR,
        )
        return Response({"cotizacion_id": cotizacion.pk}, status=201)


class SeguimientoSerializer(serializers.ModelSerializer):
    class Meta:
        model = OportunidadSeguimiento
        fields = [
            "id", "oportunidad", "vendedor", "tipo", "detalle", "fecha_recordatorio",
            "completado", "completado_at", "created_at",
        ]
        read_only_fields = ["vendedor", "completado_at", "created_at"]

    def validate_oportunidad(self, oportunidad):
        if self.instance is not None and oportunidad.pk != self.instance.oportunidad_id:
            raise serializers.ValidationError("No se puede cambiar la oportunidad de un seguimiento.")
        visibles = oportunidades_visibles(oportunidades_base(), self.context["request"].user)
        if not visibles.filter(pk=oportunidad.pk).exists():
            raise serializers.ValidationError("Oportunidad no encontrada.")
        return oportunidad


class SeguimientoViewSet(viewsets.ModelViewSet):
    """Seguimientos (nota, llamada, correo, visita, tarea) con fecha de recordatorio.

    ``?pendientes=true`` lista los no completados, por fecha de recordatorio; con ``?hasta=AAAA-MM-DD``
    solo los que tocan en esa fecha o antes (``?hasta=hoy`` usa el día de hoy).
    """

    queryset = OportunidadSeguimiento.objects.all()
    serializer_class = SeguimientoSerializer
    http_method_names = ["get", "post", "patch", "delete"]
    pagination_class = LimitOffsetPagination

    def get_queryset(self):
        visibles = oportunidades_visibles(oportunidades_base(), self.request.user).values("pk")
        qs = super().get_queryset().filter(oportunidad_id__in=visibles)
        if self.action != "list":
            return qs
        params = self.request.query_params
        if params.get("oportunidad"):
            qs = qs.filter(oportunidad_id=params["oportunidad"])
        if params.get("tipo"):
            qs = qs.filter(tipo=params["tipo"])
        pendientes = (params.get("pendientes") or "").lower() in ("1", "true", "si", "sí")
        if pendientes:
            qs = qs.filter(completado=False).exclude(oportunidad__etapa=Oportunidad.Etapa.PERDIDA)
        hasta = params.get("hasta")
        if hasta:
            fecha = timezone.localdate() if hasta == "hoy" else parse_date(hasta)
            if fecha is None:
                raise ValidationError({"hasta": "Fecha inválida (AAAA-MM-DD o 'hoy')."})
            qs = qs.filter(fecha_recordatorio__isnull=False, fecha_recordatorio__lte=fecha)
        if pendientes:
            return qs.order_by(F("fecha_recordatorio").asc(nulls_last=True), "id")
        return qs

    def perform_create(self, serializer):
        serializer.save(vendedor=self.request.user)

    def perform_update(self, serializer):
        completado = serializer.validated_data.get("completado")
        extra = {}
        if completado is not None and completado != serializer.instance.completado:
            extra["completado_at"] = timezone.now() if completado else None
        serializer.save(**extra)

    @action(detail=True, methods=["post"])
    def completar(self, request, pk=None):
        seguimiento = self.get_object()
        if not seguimiento.completado:
            seguimiento.completado, seguimiento.completado_at = True, timezone.now()
            seguimiento.save(update_fields=["completado", "completado_at"])
        return Response(self.get_serializer(seguimiento).data)

