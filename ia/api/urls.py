from django.urls import path

from ia.api.views import (
    AIAssistantAPIView,
    GoogleCalendarEventsAPIView,
    GoogleGmailMessageDetailAPIView,
    GoogleGmailMessagesAPIView,
    GoogleGmailSendAPIView,
    GoogleOAuthCallbackAPIView,
    GoogleOAuthConnectAPIView,
    GoogleOAuthDisconnectAPIView,
    GoogleOAuthStatusAPIView,
)


urlpatterns = [
    path("chat/", AIAssistantAPIView.as_view(), name="ai_chat"),
    path("google/oauth/connect/", GoogleOAuthConnectAPIView.as_view(), name="ai_google_oauth_connect"),
    path("google/oauth/callback/", GoogleOAuthCallbackAPIView.as_view(), name="ai_google_oauth_callback"),
    path("google/oauth/status/", GoogleOAuthStatusAPIView.as_view(), name="ai_google_oauth_status"),
    path("google/oauth/disconnect/", GoogleOAuthDisconnectAPIView.as_view(), name="ai_google_oauth_disconnect"),
    path("google/calendar/events/", GoogleCalendarEventsAPIView.as_view(), name="google_calendar_events"),
    path("google/gmail/messages/", GoogleGmailMessagesAPIView.as_view(), name="google_gmail_messages"),
    path("google/gmail/messages/<str:msg_id>/", GoogleGmailMessageDetailAPIView.as_view(), name="google_gmail_message_detail"),
    path("google/gmail/send/", GoogleGmailSendAPIView.as_view(), name="google_gmail_send"),
]
