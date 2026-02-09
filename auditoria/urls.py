from django.urls import path
from .views import SistemasDashboardView, LogViewerAPI, LogDownloadView

app_name = 'auditoria'

urlpatterns = [
    path('sistemas/', SistemasDashboardView.as_view(), name='sistemas_dashboard'),
    path('api/log-content/', LogViewerAPI.as_view(), name='log_content'),
    path('download/', LogDownloadView.as_view(), name='log_download'),
]
