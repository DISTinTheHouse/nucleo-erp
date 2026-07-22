"""
Autenticación DRF con validación de Origin (protección CSRF).

Contexto: todas las rutas DRF son csrf_exempt por diseño
(``APIView.as_view()``) y la autenticación por cookie JWT con
``SameSite=None`` permite que una página externa dispare peticiones de
escritura autenticadas con la cookie de un usuario logueado. Este módulo
cierra esa brecha validando el header ``Origin`` — que el navegador
adjunta a toda petición cross-origin y que una página maliciosa no puede
falsificar — contra la misma whitelist de CORS ya configurada.

El patrón replica a ``SessionAuthentication.enforce_csrf`` de DRF: la
validación vive dentro de ``authenticate()``, donde se sabe si la
credencial llegó por cookie o por header ``Authorization``.
"""

from urllib.parse import urlsplit

from django.core.exceptions import DisallowedHost

from auth_kit.authentication import (
    JWTCookieAuthentication,
    JWTCookieAuthenticationScheme,
)
from corsheaders.middleware import CorsMiddleware
from rest_framework import exceptions
from rest_framework.permissions import SAFE_METHODS

# Instancia única de CorsMiddleware usada solo como matcher de orígenes,
# para reutilizar exactamente la misma lógica de django-cors-headers
# (CORS_ALLOWED_ORIGINS + CORS_ALLOWED_ORIGIN_REGEXES) sin duplicarla.
# Lee la configuración de settings en cada llamada, así ambas listas se
# mantienen sincronizadas automáticamente.
_cors_matcher = CorsMiddleware(lambda request: None)

ORIGIN_DENIED_MESSAGE = "Origen no permitido para esta solicitud."


class OriginEnforcedJWTCookieAuthentication(JWTCookieAuthentication):
    """
    Extiende ``JWTCookieAuthentication`` exigiendo un ``Origin`` permitido
    cuando la petición fue autenticada vía cookie y el método no es seguro.

    Reglas:
    - Métodos seguros (GET/HEAD/OPTIONS): sin validación (no son
      relevantes para CSRF).
    - Autenticación vía header ``Authorization`` (Bearer): sin validación
      (un atacante cross-site no puede adjuntar headers, es inmune a CSRF
      por diseño).
    - Autenticación vía cookie + método de escritura: el header ``Origin``
      debe estar presente y coincidir con la whitelist de CORS o con el
      propio origen del backend (peticiones same-origin, p. ej. Swagger UI
      en /api/docs/).
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            # Petición no autenticada: no hay sesión que un atacante
            # pueda montar, la capa de permisos decide.
            return None
        if request.method in SAFE_METHODS:
            return result
        if self.get_header(request) is not None:
            # La credencial llegó por header Authorization, no por cookie
            # (el header tiene prioridad en authenticate_with_cookie).
            return result
        self.enforce_origin(request)
        return result

    def enforce_origin(self, request):
        """Valida el Origin de una petición de escritura autenticada por cookie."""
        origin = request.META.get("HTTP_ORIGIN")
        if not origin:
            raise exceptions.PermissionDenied(ORIGIN_DENIED_MESSAGE)

        # Peticiones same-origin (servidas por el propio backend).
        if origin == self._request_origin(request):
            return

        try:
            url = urlsplit(origin)
        except ValueError:
            raise exceptions.PermissionDenied(ORIGIN_DENIED_MESSAGE)

        if _cors_matcher.origin_found_in_white_lists(origin, url):
            return

        raise exceptions.PermissionDenied(ORIGIN_DENIED_MESSAGE)

    @staticmethod
    def _request_origin(request):
        try:
            return f"{request.scheme}://{request.get_host()}"
        except DisallowedHost:
            return None


class OriginEnforcedJWTCookieAuthenticationScheme(JWTCookieAuthenticationScheme):
    """Registra en drf-spectacular el mismo esquema OpenAPI que la clase base."""

    target_class = "nucleo.authentication.OriginEnforcedJWTCookieAuthentication"
