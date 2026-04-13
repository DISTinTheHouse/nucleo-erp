from django.urls import path

from ia.api.views import AIAssistantAPIView, GoogleCalendarEventsAPIView


urlpatterns = [
    path("chat/", AIAssistantAPIView.as_view(), name="ai_chat"),
    path("google/calendar/events/", GoogleCalendarEventsAPIView.as_view(), name="google_calendar_events"),
]
