import requests
from django.conf import settings
from .exceptions import FacturamaAPIException

class FacturamaClient:

    def __init__(self):
        print(settings)
        self.base_url = settings.FACTURAMA_BASE_URL
        self.username = settings.FACTURAMA_USERNAME
        self.password = settings.FACTURAMA_PASSWORD
        self.timeout = int(settings.FACTURAMA_TIMEOUT)

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("auth",(self.username, self.password))
        headers = kwargs.pop("headers", {})
        headers.setdefault("Accept", "application/json")

        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")

        try:
            res = requests.request(
                method=method,
                url=url,
                headers=headers,
                **kwargs
            )
        except requests.Timeout:
            raise FacturamaAPIException(
                message="Facturama request timed out.",
                code="FACTURAMA_TIMEOUT"
            )

        except requests.ConnectionError:
             raise FacturamaAPIException(
                message="Could not connect to Facturama.",
                code="FACTURAMA_CONNECTION_ERROR"
            )

        except requests.RequestException as exc:                    
            raise FacturamaAPIException(
                message="Error communicating with Facturama.",
                code="FACTURAMA_REQUEST_ERROR",
                details=str(exc)
            )

        return self._handle_response(res)
            
    def _handle_response(self, response):
        try:
            data = response.json()
        except ValueError:
            data = response.text

        if not response.ok:
            raise FacturamaAPIException(
                message="Facturama API returned an error.",
                code="FACTURAMA_API_ERROR",
                status_code=response.status_code,
                details=data
            )

        return data

    def get(self, endpoint, **kwargs):
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint, **kwargs):
        return self._request("POST", endpoint, **kwargs)

    def put(self, endpoint, **kwargs):
        return self._request("PUT", endpoint, **kwargs)

    def delete(self, endpoint, **kwargs):
        return self._request("DELETE", endpoint, **kwargs)    
        
        