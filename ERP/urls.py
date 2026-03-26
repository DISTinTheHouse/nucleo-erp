from django.contrib import admin
from django.urls import path, include
from django.contrib.auth.views import LoginView
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    # ...
    # Swagger / OpenAPI
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    # ...
    path('', LoginView.as_view(template_name='registration/login.html', redirect_authenticated_user=True), name='login'),
    path('', include('nucleo.urls')),
    path('', include('usuarios.urls')),
    path('', include(('seguridad.urls', 'seguridad'), namespace='seguridad')),
    path('', include('inventarios.urls')),
    path('api/v1/catalogo/', include('catalogo.api.urls')),
    path('api/v1/inventarios/', include('inventarios.api.urls')),
    path('api/v1/terceros/', include('terceros.api.urls')),
    path('api/v1/compras/', include('compras.api.urls')),
    path('api/v1/produccion/', include('produccion.api.urls')),
    path('api/v1/ventas/', include('ventas.api.urls')),
    path('api/v1/ai/', include('ia.api.urls')),
    path('auditoria/', include('auditoria.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
]
