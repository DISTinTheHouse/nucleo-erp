import json
import time
from datetime import datetime

from django.db.models import Max
from django.http import StreamingHttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.request import Request

from ..models import Notificacion
from ..services.notificacion_service import (
    _aplicar_scope_empresa,
    notificaciones_para_usuario,
    marcar_leida as marcar_leida_svc,
    marcar_todas_leidas as marcar_todas_leidas_svc,
)
from .serializers import NotificacionSerializer


SSE_TICK_SECONDS = 3
SSE_KEEPALIVE_SECONDS = 20
SSE_MAX_ITERATIONS = 120


def _autenticar_por_token_param(request):
    """Valida un JWT pasado por query param `token` para endpoints SSE
    (EventSource no soporta Authorization header). Reutiliza la misma clase
    de autenticación del proyecto (OriginEnforcedJWTCookieAuthentication)
    que delega en simpleJWT para validar la firma/expiración."""
    token_raw = request.GET.get("token")
    if not token_raw:
        return None

    from nucleo.authentication import OriginEnforcedJWTCookieAuthentication
    from rest_framework.request import Request as DRFRequest
    from rest_framework.parsers import JSONParser

    request.META.setdefault("HTTP_AUTHORIZATION", f"Bearer {token_raw}")

    wrapped = (
        request
        if isinstance(request, DRFRequest)
        else DRFRequest(request, parsers=[JSONParser()])
    )

    authenticator = OriginEnforcedJWTCookieAuthentication()
    result = authenticator.authenticate(wrapped)
    if result is None:
        return None
    user, _ = result
    return user


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

    @action(detail=False, methods=["get"], url_path="stream")
    def stream(self, request):
        """Server-Sent Events: stream de notificaciones nuevas en tiempo real.

        URL ejemplo:
            GET /api/v1/notificaciones/stream/?token=<JWT_ACCESS_TOKEN>

        Eventos que emite:
        - `event: nueva`     → data: {count, ultima_notificacion_id}  (hay una o más notificaciones nuevas)
        - `event: ping`      → data: {t, count}                       (keepalive cada ~20s)
        - `event: error`     → data: {code, message}                  (token inválido o expirado, cierra stream)
        """
        from django.db import close_old_connections

        user = _autenticar_por_token_param(request)
        if user is None or getattr(user, "is_active", True) is False:
            payload = {
                "code": "AUTH_REQUIRED",
                "message": "Token inválido, expirado o sin permiso.",
            }
            return StreamingHttpResponse(
                streaming_content=iter(
                    [f"event: error\ndata: {json.dumps(payload)}\n\n"]
                ),
                content_type="text/event-stream; charset=utf-8",
            )

        qs_base = Notificacion.objects.filter(usuario=user, leido=False)
        qs_base = _aplicar_scope_empresa(qs_base, user, lookup="empresa")

        def generar_eventos():
            ultimo_count = qs_base.count()
            ultimo_max_id = qs_base.aggregate(m=Max("id"))["m"] or 0
            ultimo_keepalive = time.monotonic()

            yield (
                f"event: abierto\n"
                f"data: {json.dumps({'count': ultimo_count, 'ultima_notificacion_id': ultimo_max_id, 'server_time': datetime.utcnow().isoformat() + 'Z'})}\n\n"
            )

            for _ in range(SSE_MAX_ITERATIONS):
                close_old_connections()

                count = qs_base.count()
                max_id = qs_base.aggregate(m=Max("id"))["m"] or 0

                if max_id > ultimo_max_id or count != ultimo_count:
                    ultimo_count = count
                    ultimo_max_id = max_id
                    yield (
                        f"event: nueva\n"
                        f"data: {json.dumps({'count': count, 'ultima_notificacion_id': max_id})}\n\n"
                    )
                    ultimo_keepalive = time.monotonic()

                now = time.monotonic()
                if now - ultimo_keepalive >= SSE_KEEPALIVE_SECONDS:
                    yield (
                        f"event: ping\n"
                        f"data: {json.dumps({'t': datetime.utcnow().isoformat() + 'Z', 'count': count})}\n\n"
                    )
                    ultimo_keepalive = now

                time.sleep(SSE_TICK_SECONDS)

            yield f"event: fin\ndata: {json.dumps({'reconnect_ms': 1000})}\n\n"

        resp = StreamingHttpResponse(
            streaming_content=generar_eventos(),
            content_type="text/event-stream; charset=utf-8",
        )
        resp["Cache-Control"] = "no-cache, no-transform"
        resp["Connection"] = "keep-alive"
        resp["X-Accel-Buffering"] = "no"
        return resp
