from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('nucleo.urls')),
    path('', include('usuarios.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
]
