import logging
import time
from django.conf import settings

logger = logging.getLogger('api_logger')

def get_client_ip(request):
    if getattr(settings, "IS_VERCEL", False) or str(getattr(settings, "ENVIRONMENT", "") or "").lower() == "production":
        forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip()

class APILoggingMiddleware:
    """
    Middleware to log all API requests to a separate log file.
    Captures: Method, Path, User, Status Code, Duration.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Only log /api/ requests
        if not request.path.startswith('/api/'):
            return self.get_response(request)

        start_time = time.time()
        
        # Process request
        response = self.get_response(request)
        
        duration = time.time() - start_time
        
        user = request.user if request.user.is_authenticated else 'Anonymous'
        status_code = response.status_code
        method = request.method
        path = request.path
        
        log_message = f"User: {user} | Method: {method} | Path: {path} | Status: {status_code} | Duration: {duration:.4f}s"
        
        if 200 <= status_code < 300:
            logger.info(log_message)
        elif 400 <= status_code < 500:
            logger.warning(log_message)
        else:
            logger.error(log_message)

        return response

class NoCacheMiddleware:
    """
    Middleware para deshabilitar el caché en todas las respuestas de la API.
    Añade headers: Cache-Control: no-store, no-cache, must-revalidate, max-age=0
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        
        # Aplicar solo a rutas de API
        if request.path.startswith('/api/'):
            response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'
            
        return response
