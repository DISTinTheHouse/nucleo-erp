from django.urls import path
from . import views

urlpatterns = [
    path('creator/', views.creator, name='ia_creator'),
    path('drive/', views.drive, name='drive'),
    path('drive/google/connect/', views.drive_google_connect, name='drive_google_connect'),
    path('drive/google/callback/', views.drive_google_callback, name='drive_google_callback'),
    path('drive/disconnect/<str:provider>/', views.drive_disconnect, name='drive_disconnect'),
    path('correo/', views.correo, name='correo'),
    path('correo/send/', views.correo_send, name='correo_send'),
]
