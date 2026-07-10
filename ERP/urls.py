from pathlib import Path

from django.contrib import admin
from django.http import FileResponse, Http404
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from django.contrib.auth.views import LoginView
from terceros.views import RfcStatusView, ClientCreateView


def serve_favicon(_request):
    favicon_path = Path(__file__).resolve().parent.parent / "favicon.ico"
    if not favicon_path.exists():
        raise Http404("Favicon no encontrado.")
    return FileResponse(open(favicon_path, "rb"), content_type="image/x-icon")

urlpatterns = [
    path('admin/', admin.site.urls),
    path('favicon.ico', serve_favicon, name='favicon-ico'),
    path('favicon.png', serve_favicon, name='favicon-png'),
    # ...
    # Swagger / OpenAPI
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    # login 
    path('', LoginView.as_view(template_name='registration/login.html', redirect_authenticated_user=True), name='login'),
    path('api/auth/', include('auth_kit.urls')),
    # ...
    path('core/terceros/validar-rfc/', RfcStatusView.as_view(), name='rfc_status'),
    path('core/terceros/crear-cliente/', ClientCreateView.as_view(), name='facturama_cliente_create'),
    path('', include('nucleo.urls')),
    path('', include('usuarios.urls')),
    path('', include(('seguridad.urls', 'seguridad'), namespace='seguridad')),
    path('', include('inventarios.urls')),
    path('ia/', include('ia.urls')),
    path('api/v1/catalogo/', include('catalogo.api.urls')),
    path('api/v1/inventarios/', include('inventarios.api.urls')),
    path('api/v1/terceros/', include('terceros.api.urls')),
    path('api/v1/compras/', include('compras.api.urls')),
    path('api/v1/produccion/', include('produccion.api.urls')),
    path('api/v1/ventas/', include('ventas.api.urls')),
    path('api/v1/finanzas/', include('finanzas.api.urls')),
    path('api/v1/ai/', include('ia.api.urls')),
    path('api/v1/wms/', include('wms.api.urls')),
    path('auditoria/', include('auditoria.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
    path('QA/', include('QA.urls')),
]
