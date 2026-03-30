from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from usuarios.views import TwoFactorLoginView, TwoFactorVerifyView

urlpatterns = [
    path('admin/', admin.site.urls),
    # ...
    # Swagger / OpenAPI
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    # ...
    path('', TwoFactorLoginView.as_view(), name='login'),
    path('two-factor/', TwoFactorVerifyView.as_view(), name='two_factor_verify'),
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
    path('api/v1/ai/', include('ia.api.urls')),
    path('auditoria/', include('auditoria.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
]
