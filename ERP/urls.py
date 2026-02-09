from django.contrib import admin
from django.urls import path, include
from django.contrib.auth.views import LoginView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', LoginView.as_view(template_name='registration/login.html', redirect_authenticated_user=True), name='login'),
    path('', include('nucleo.urls')),
    path('', include('usuarios.urls')),
    path('', include('seguridad.urls')),
    path('auditoria/', include('auditoria.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
]
