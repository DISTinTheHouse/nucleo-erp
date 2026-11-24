from django.db.models import Max
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from nucleo.listados import BusquedaSoloListadoFilter, OrdenamientoEstableFilter, PaginacionOpcionalLimitOffset

from ..models import Notificacion
from ..services.notificacion_service import (
    notificaciones_para_usuario,
    marcar_leida as marcar_leida_svc,
    marcar_todas_leidas as marcar_todas_leidas_svc,
)
from .serializers import NotificacionSerializer


class NotificacionViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = NotificacionSerializer
    http_method_names = ["get", "post"]
    # Sin ?limit= regresa la lista completa; ?limit= inválido → 400 y tope de 500 (#950).
    pagination_class = PaginacionOpcionalLimitOffset
    # ``?search=`` busca en título y mensaje (#1346); antes se aceptaba y se ignoraba
    # porque no había ``search_fields``. ``?ordering=`` acepta los mismos campos que
    # antes, pero con ``id`` de desempate: solo, un campo de pocos valores (``leido``,
    # ``modulo``) dejaba páginas que se enciman o se saltan filas (#1345). Sin
    # ``?ordering=`` válido se queda el ``-created_at`` de ``get_queryset``.
    filter_backends = [DjangoFilterBackend, BusquedaSoloListadoFilter, OrdenamientoEstableFilter]
    search_fields = ["titulo", "mensaje"]
    ordering_fields = ["id", "titulo", "mensaje", "modulo", "tipo", "leido", "leido_at", "data", "created_at"]

    def get_queryset(self):
        user = self.request.user
        qs = Notificacion.objects.all()
        # El único alcance es el destinatario. Un filtro extra por
        # ``Notificacion.empresa`` no añade aislamiento —``usuario`` ya es más
        # estricto— y sí quitaba filas propias: las de un usuario multiempresa
        # creadas para otra de sus empresas, y TODAS las de un superusuario sin
        # ``empresa`` asignada. Mismo criterio que ``marcar_todas_leidas()``.
        qs = notificaciones_para_usuario(qs, user)

        leido_param = self.request.query_params.get("leido")
        if leido_param is not None and str(leido_param).lower() in {
            "true",
            "false",
            "1",
            "0",
        }:
            qs = qs.filter(leido=str(leido_param).lower() in {"true", "1"})

        modulo = self.request.query_params.get("modulo")
        if modulo:
            qs = qs.filter(modulo__iexact=modulo)

        return qs.order_by("-created_at")

    @action(detail=False, methods=["get"], url_path="sin-leer/count")
    def sin_leer_count(self, request):
        """Contador de no leídas: la vía para refrescar la campana (consultarlo cada
        cierto tiempo). Ya no hay ``stream/`` (SSE): pedía el JWT en la URL y
        ocupaba la función y la BD ~6 min por conexión (#1315)."""
        qs = self.get_queryset().filter(leido=False)
        count = qs.count()
        ultima = (
            qs.aggregate(max_id=Max("id"))["max_id"] or 0
        )
        return Response({"count": count, "ultima_notificacion_id": ultima})

    @action(detail=True, methods=["post"], url_path="marcar-leida")
    def marcar_leida(self, request, pk=None):
        notif = self.get_object()
        marcar_leida_svc(notif, request.user)
        return Response(self.get_serializer(notif).data)

    @action(detail=False, methods=["post"], url_path="marcar-todas-leidas")
    def marcar_todas_leidas(self, request):
        n = marcar_todas_leidas_svc(request.user)
        return Response({"actualizadas": n})
