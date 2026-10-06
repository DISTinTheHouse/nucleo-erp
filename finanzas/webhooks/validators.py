import base64
import jwt
from django.conf import settings
from .exceptions import SyncfyWebhookException

def verfy_syncfy_signature(signature: str):
    webhook_signature_key = settings.SYNCFY_WEBHOOK_SIGNATURE_KEY
    
    if not webhook_signature_key:
        raise SyncfyWebhookException(
            message='Something went wrong.', 
            code='SERVER_ERROR',
            status_code=500,
        )

    try:
        key = base64.b64decode(webhook_signature_key)
    except Exception as exc:
        raise SyncfyWebhookException(
            message='Something went wrong.', 
            code='SERVER_ERROR',
            status_code=500
        )

    try:
        decoded = jwt.decode(
            signature,
            key,
            algorithms=["HS256"]
        )

        return decoded
    
    except jwt.InvalidTokenError:
        raise SyncfyWebhookException(
            message='Invalid Syncfy webhook signature.', 
            code='INVALID_SIGNATURE',
            status_code=500
        )
 