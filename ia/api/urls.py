from django.urls import path

from ia.api.views import AIAssistantAPIView


urlpatterns = [
    path("chat/", AIAssistantAPIView.as_view(), name="ai_chat"),
]
