from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpResponse, Http404, JsonResponse
from django.conf import settings
import os

class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

class SistemasDashboardView(LoginRequiredMixin, SuperuserRequiredMixin, TemplateView):
    template_name = 'auditoria/dashboard.html'

class LogViewerAPI(LoginRequiredMixin, SuperuserRequiredMixin, TemplateView):
    """
    API para leer las últimas N líneas de un archivo de log.
    Query Params:
    - type: 'sistema', 'api', 'auditoria'
    - lines: int (default 100)
    """
    def get(self, request, *args, **kwargs):
        log_type = request.GET.get('type', 'sistema')
        try:
            num_lines = int(request.GET.get('lines', 100))
        except ValueError:
            num_lines = 100
        
        log_map = {
            'sistema': 'sistema.log',
            'api': 'api.log',
            'auditoria': 'auditoria.log'
        }
        
        filename = log_map.get(log_type)
        if not filename:
            return JsonResponse({'error': 'Tipo de log inválido'}, status=400)
            
        file_path = settings.LOGS_DIR / filename
        
        if not os.path.exists(file_path):
            return JsonResponse({'content': 'El archivo de log aún no existe o está vacío.'})
            
        try:
            # Leer las últimas N líneas de manera eficiente
            # Para archivos pequeños/medianos, readlines() es aceptable.
            # Para rotación, solo leemos el actual.
            # Usamos errors='replace' para evitar fallos si hay caracteres extraños
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
                last_lines = lines[-num_lines:]
                content = "".join(last_lines)
                return JsonResponse({'content': content})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

class LogDownloadView(LoginRequiredMixin, SuperuserRequiredMixin, TemplateView):
    def get(self, request, *args, **kwargs):
        log_type = request.GET.get('type')
        log_map = {
            'sistema': 'sistema.log',
            'api': 'api.log',
            'auditoria': 'auditoria.log'
        }
        
        filename = log_map.get(log_type)
        if not filename:
            raise Http404("Tipo de log no encontrado")
            
        file_path = settings.LOGS_DIR / filename
        
        if not os.path.exists(file_path):
            raise Http404("El archivo de log no existe")
            
        with open(file_path, 'rb') as f:
            response = HttpResponse(f.read(), content_type='text/plain')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
