from django.db.models import Count
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import Notificacion
from ..services.notificacion_service import (
    _aplicar_scope_empresa,
    notificaciones_para_usuario,
    marcar_leida as marcar_leida_svc,
    marcar_todas_leidas as marcar_todas_leidas_svc,
)
from .serializers import NotificacionSerializer


class NotificacionViewSet(viewsets.ModelViewSet):
    serializer_class = NotificacionSerializer
    http_method_names = ["get", "post"]

    def get_queryset(self):
        user = self.request.user
        qs = Notificacion.objects.all()
        qs = _aplicar_scope_empresa(qs, user, lookup="empresa")
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
        count = (
            self.get_queryset().filter(leido=False).aggregate(total=Count("id"))["total"]
            or 0
        )
        return Response({"count": count})

    @action(detail=True, methods=["post"], url_path="marcar-leida")
    def marcar_leida(self, request, pk=None):
        notif = self.get_object()
        marcar_leida_svc(notif, request.user)
        return Response(self.get_serializer(notif).data)

    @action(detail=False, methods=["post"], url_path="marcar-todas-leidas")
    def marcar_todas_leidas(self, request):
        n = marcar_todas_leidas_svc(request.user)
        return Response({"actualizadas": n})
