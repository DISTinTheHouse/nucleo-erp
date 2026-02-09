import logging
import time

logger = logging.getLogger('api_logger')

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
