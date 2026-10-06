import hashlib
from datetime import datetime
from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from finanzas.models import SyncfyWebhookNotification, SyncfyWebhookEvent
from .validators import verfy_syncfy_signature


class SyncfyWebhookView(APIView):
    authentication_classes = []
    permission_classes = []

    ALLOWED_EVENTS = {
        "credentials.created",
        "credentials.updated",
        "credentials.refreshed",
        "documents.completed",
        "documents.fail",
        "documents.success",
    }

    def post(self, request):
        signature = request.headers.get("request-signature")

        if not signature:
            return Response({"detail": "Missing request-signature header."}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            verfy_syncfy_signature(signature=signature)
        except ValueError as exc:
            return Response({"status": "unsigned", "detail": str(exc)}, status=status.HTTP_401_UNAUTHORIZED)

        raw_body = request.body
        body_hash = hashlib.sha256(raw_body).hexdigest()

        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Invalid webhook payload."}, status=status.HTTP_400_BAD_REQUEST)

        events = data.get("events")

        if not isinstance(events, list):
            return Response({"detail": "The 'events' field must be a list."}, status=status.HTTP_400_BAD_REQUEST,)

        if not events:
            return Response({"detail": "Webhook contains no events."}, status=status.HTTP_400_BAD_REQUEST,)

        if SyncfyWebhookNotification.objects.filter(body_hash=body_hash).exists():
            return Response({"status": "already_received",}, status=status.HTTP_200_OK)
        
        try:
            with transaction.atomic():
                rid = data.get("rid")

                notification = (
                    SyncfyWebhookNotification.objects.create(
                        rid=rid,
                        body_hash=body_hash,
                        payload=data,
                        status=(SyncfyWebhookNotification.Status.RECEIVED),
                    )
                )

                for event_data in events:
                    header = event_data.get("header", {})
                    event_header = header.get("event", {},)
                    event_payload = event_data.get("payload", {})
                    event_name = event_header.get("name")
                    event_id = event_header.get("eid")

                    if not event_name: raise ValueError("Webhook event has no event name.")
                    if not event_id: raise ValueError("Webhook event has no event id.")
                    if event_name not in self.ALLOWED_EVENTS: raise ValueError(f"Unsupported Syncfy event: "f"{event_name}")

                    event_at = event_header.get("at")

                    if event_at: event_at = parse_datetime(event_at)

                    SyncfyWebhookEvent.objects.create(
                        notification=notification,
                        event_id=event_id,
                        event_name=event_name,
                        event_version=(event_header.get("version", "")),
                        event_at=event_at,
                        id_environment=(header.get("user", {}).get("id_environment", "")),
                        id_external=(header.get("user", {}).get("id_external", "")),
                        id_user=(header.get("user", {}).get("id_user", "")),
                        id_credential=(event_payload.get("id_credential", "")),
                        payload=event_payload,
                    )

        except IntegrityError:
            if SyncfyWebhookNotification.objects.filter(body_hash=body_hash).exists():
                return Response({"status": "already_received",}, status=status.HTTP_200_OK)

        except ValueError as exc:
            if "notification" in locals():
                notification.status = (SyncfyWebhookNotification.Status.FAILED)
                notification.error_message = str(exc)
                notification.save(update_fields=["status","error_message"])
            return Response({"detail": str(exc),}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"status": "received", "id": str(notification.id)}, status=status.HTTP_200_OK)


