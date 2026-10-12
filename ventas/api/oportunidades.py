"""Oportunidades: lo que pasa antes de la cotización.

Cotizada y Ganada no se guardan, se calculan de las cotizaciones ligadas.
"""

from decimal import Decimal

from django.db.models import Case, CharField, Count, Exists, F, OuterRef, Q, Sum, Value, When
from django.utils.dateparse import parse_date
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response

from ventas.models import Cotizacion, Oportunidad
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
