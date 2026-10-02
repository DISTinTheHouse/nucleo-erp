import requests
from django.conf import settings
from .exceptions import SyncfyAPIException

class SyncfyClient:
    def __init__(self):
        self.base = settings.SYNCFY_BASE_URL
        self.api_key = settings.SYNCFY_APIKEY

    def _headers(self):
        return {
            "Authorization": f"api_key api_key={self.api_key}",
            "Content-Type": "application/json"
        }

    def _handle_response(self, response):
        try:
            data = response.json()
        except ValueError:
            data = response.text

        if not response.ok:
            raise SyncfyAPIException(
                message="Syncfy API returned an error.",
                code="SYNCFY_API_ERROR",
                status_code=response.status_code,
                details=data
            )
        
        return data

    def _request(self, method, path, **kw):
        try:
            res = requests.request(
                method=method, 
                url=f"{self.base}/{path.lstrip('/')}", 
                headers=self._headers(), 
                timeout=30,
                **kw
            )
        except requests.Timeout:
            raise SyncfyAPIException(
                message="Syncfy request timed out.",
                code="SYNCFY_TIMEOUT"
            )
        except requests.ConnectionError:
            raise SyncfyAPIException(
                message="Could not connect to Syncfy",
                code="SYNCFY_CONNECTION_ERROR"
            )
        except requests.RequestException as exc:
            raise SyncfyAPIException(
                message="Error communicating with Syncfy.",
                code="SYNCFY_REQUEST_ERROR",
                details=str(exc)
            )

        return self._handle_response(res)

    def create_user(self, payload):
        return self._request(method='POST', path='/v1/users', json=payload)

    def create_session(self, syncfy_user_id):
        return self._request(method='POST', path='/v1/sessions', json={'id_user': syncfy_user_id})


    