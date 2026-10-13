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
from terceros.scope import clientes_base, clientes_visibles, ve_clientes_sin_asignacion
from usuarios.models import Usuario
from ventas.models import Cotizacion, Oportunidad, OportunidadSeguimiento, Pedido
from ventas.scope import oportunidades_base, oportunidades_visibles

ESTATUS_BORRADOR = 1
ESTATUS_AUTORIZADA = 3
ESTADOS = ["Nueva", "Contactada", "Cotizada", "Ganada", "Perdida"]
ID_MAXIMO = 2**63 - 1  # tope de un BigAutoField


def _id_valido(valor, nombre):
    """Id que llega en un query string o en un body; si no lo es responde 400, no 500.

    Acepta un entero o un texto de solo dígitos ASCII, dentro del rango de un id. ``int()``
    también acepta ``True``, decimales, espacios, signo, ``1_0`` y dígitos de otros
    alfabetos, y un valor enorme truena en la consulta.
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, str)):
        raise ValidationError({nombre: "Debe ser un número entero."})
    texto = str(valor)
    if not (texto.isascii() and texto.isdigit()) or len(texto) > 19 or int(texto) > ID_MAXIMO:
        raise ValidationError({nombre: "Debe ser un número entero."})
    return int(texto)


def _entero(params, nombre):
    """Parámetro numérico del query string; ``None`` si no viene."""
    valor = params.get(nombre)
    if not valor:
        return None
    return _id_valido(valor, nombre)


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
        if cliente is None:
            return cliente
        visible = clientes_visibles(clientes_base(), user).filter(pk=cliente.pk).exists()
        if cliente.empresa_id != empresa_id or not visible:
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
    # Explícitos: sin esto DRF acepta en ``?ordering=`` los ``source`` del serializer, y
    # ``vendedor__get_full_name`` no es una columna (500). Son los que ya se aceptaban.
    ordering_fields = [
        "id", "cliente", "cliente__nombre", "vendedor", "nombre_contacto", "telefono", "correo",
        "titulo", "monto_estimado", "etapa", "motivo_perdida", "proximo_contacto", "notas",
        "created_at", "updated_at",
    ]

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
        if _entero(params, "cliente") is not None:
            qs = qs.filter(cliente_id=_entero(params, "cliente"))
        if _entero(params, "vendedor") is not None:
            qs = qs.filter(vendedor_id=_entero(params, "vendedor"))
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
        # Misma validación que los parámetros del query string; el id llega como número o texto.
        dato = request.data.get("vendedor")
        vendedor_id = 0 if dato in (None, "") else _id_valido(dato, "vendedor")
        vendedor = Usuario.objects.filter(
            pk=vendedor_id, empresa_id=oportunidad.empresa_id, is_active=True
        ).first()
        if vendedor is None:
            raise ValidationError({"vendedor": "Vendedor no encontrado en la empresa."})
        oportunidad.vendedor = vendedor
        oportunidad.save(update_fields=["vendedor", "updated_at"])
        return Response(self.get_serializer(oportunidad).data)

    def _cartera(self, oportunidad):
        """Cartera del cliente, solo si el usuario puede ver a ese cliente."""
        if not oportunidad.cliente_id:
            return []
        visible = clientes_visibles(clientes_base(), self.request.user).filter(pk=oportunidad.cliente_id).exists()
        return cartera_de_cliente(oportunidad.cliente) if visible else []

    @action(detail=True, methods=["get"])
    def onboarding(self, request, pk=None):
        """Todo lo de la pantalla de una oportunidad en una llamada: datos, seguimientos,
        cotizaciones (con su pedido) y cartera del cliente."""
        oportunidad = self.get_object()
        seguimientos = OportunidadSeguimiento.objects.filter(oportunidad=oportunidad).select_related("vendedor")
        pendientes = seguimientos.filter(completado=False)
        # Todos son de esta oportunidad: se reusa la ya cargada en vez de reconstruirla por fila.
        recientes = list(seguimientos[:50])
        for seguimiento in recientes:
            seguimiento.oportunidad = oportunidad
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
            "seguimientos": SeguimientoSerializer(
                recientes, many=True, context=self.get_serializer_context()
            ).data,
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
            "cartera": self._cartera(oportunidad),
        })

    @action(detail=True, methods=["post"])
    def cotizar(self, request, pk=None):
        """Crea una cotización en Borrador ligada a la oportunidad. Desde ahí sigue el flujo de siempre."""
        oportunidad = self.get_object()
        if oportunidad.etapa == Oportunidad.Etapa.PERDIDA:
            raise ValidationError({"etapa": "No se cotiza una oportunidad perdida."})
        sucursal = getattr(request.user, "sucursal_default", None)
        if sucursal is not None and sucursal.empresa_id != oportunidad.empresa_id:
            raise ValidationError({"sucursal": "Tu sucursal no pertenece a la empresa de la oportunidad."})
        cotizacion = Cotizacion.objects.create(
            empresa_id=oportunidad.empresa_id,
            vendedor=request.user,
            sucursal=sucursal,
            cliente_id=oportunidad.cliente_id,
            oportunidad=oportunidad,
            estatus=ESTATUS_BORRADOR,
        )
        return Response({"cotizacion_id": cotizacion.pk}, status=201)


class SeguimientoSerializer(serializers.ModelSerializer):
    # Contexto para pintar la fila de la agenda sin pedir cada oportunidad. Los nombres
    # salen igual que en ``OportunidadSerializer``, salvo el del cliente, que aquí solo
    # sale si el usuario puede ver a ese cliente.
    oportunidad_titulo = serializers.CharField(source="oportunidad.titulo", read_only=True)
    oportunidad_nombre_contacto = serializers.CharField(source="oportunidad.nombre_contacto", read_only=True)
    oportunidad_cliente_nombre = serializers.SerializerMethodField()
    vendedor_nombre = serializers.CharField(source="vendedor.get_full_name", read_only=True, default=None)

    class Meta:
        model = OportunidadSeguimiento
        fields = [
            "id", "oportunidad", "oportunidad_titulo", "oportunidad_nombre_contacto",
            "oportunidad_cliente_nombre", "vendedor", "vendedor_nombre", "tipo", "detalle",
            "fecha_recordatorio", "completado", "completado_at", "created_at",
        ]
        read_only_fields = ["vendedor", "completado_at", "created_at"]

    def get_oportunidad_cliente_nombre(self, obj) -> str | None:
        cliente = obj.oportunidad.cliente
        if cliente is None:
            return None
        visibles = self._clientes_visibles(obj)
        if visibles is not None and cliente.pk not in visibles:
            return None
        return cliente.nombre

    def _clientes_visibles(self, obj):
        """Ids de los clientes que ve el usuario, o ``None`` si no hay que filtrar.

        El superusuario y el admin de empresa ven el nombre siempre, también el de un
        cliente dado de baja, y no cuestan consulta. Al resto se le aplica la regla de la
        cartera del onboarding, solo sobre los clientes de las filas que se serializan:
        una consulta por respuesta, no por fila (se guarda en el serializer de la lista).
        """
        request = self.context.get("request")
        if request is None:
            return frozenset()
        if ve_clientes_sin_asignacion(request.user):
            return None
        lista = self.parent if isinstance(self.parent, serializers.ListSerializer) else None
        if lista is not None and hasattr(lista, "_clientes_visibles_ids"):
            return lista._clientes_visibles_ids
        filas = lista.instance if lista is not None else [obj]
        ids = {fila.oportunidad.cliente_id for fila in filas} - {None}
        visibles = frozenset(
            clientes_visibles(clientes_base(), request.user).filter(pk__in=ids).values_list("pk", flat=True)
        )
        if lista is not None:
            lista._clientes_visibles_ids = visibles
        return visibles

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
    ``?oportunidad_vendedor=<id>`` deja solo los de las oportunidades de ese vendedor.
    """

    queryset = OportunidadSeguimiento.objects.select_related("oportunidad__cliente", "vendedor")
    serializer_class = SeguimientoSerializer
    http_method_names = ["get", "post", "patch", "delete"]
    pagination_class = LimitOffsetPagination
    # Explícitos: sin esto DRF acepta en ``?ordering=`` los ``source`` del serializer, y
    # ``vendedor__get_full_name`` no es una columna (500).
    ordering_fields = [
        "id", "oportunidad", "vendedor", "tipo", "detalle", "fecha_recordatorio",
        "completado", "completado_at", "created_at",
    ]

    def get_queryset(self):
        visibles =oportunidades_visibles(oportunidades_base(), self.request.user).values("pk")
        qs = super().get_queryset().filter(oportunidad_id__in=visibles)
        if self.action != "list":
            return qs
        params = self.request.query_params
        if _entero(params, "oportunidad") is not None:
            qs = qs.filter(oportunidad_id=_entero(params, "oportunidad"))
        # Dueño de la oportunidad, no autor del seguimiento. Filtra sobre lo ya visible:
        # nunca amplía el alcance.
        if _entero(params, "oportunidad_vendedor") is not None:
            qs = qs.filter(oportunidad__vendedor_id=_entero(params, "oportunidad_vendedor"))
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

