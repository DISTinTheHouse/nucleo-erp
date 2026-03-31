from rest_framework import viewsets
from django.conf import settings
from terceros.models import Proveedor, Cliente, DireccionCliente
from terceros.api.serializers import ProveedorSerializer, ClienteSerializer, DireccionClienteSerializer
import json
import base64
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

class ClienteViewSet(viewsets.ModelViewSet):
    queryset = Cliente.objects.filter(activo=True)
    serializer_class = ClienteSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            qs = qs.filter(empresa=empresa)
            if getattr(user, "is_admin_empresa", False):
                return qs
            return qs.filter(vendedores__id=getattr(user, "id", None))
        return qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        cliente = serializer.save(empresa=empresa)
        try:
            if getattr(user, "id", None):
                cliente.vendedores.add(user)
        except Exception:
            pass
        try:
            base_url = getattr(settings, "FACTURAMA_BASE_URL", "https://apisandbox.facturama.mx").rstrip("/")
            url = f"{base_url}/Client"
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            usern = (getattr(settings, "FACTURAMA_USERNAME", "") or "").strip()
            pwd = (getattr(settings, "FACTURAMA_PASSWORD", "") or "").strip()
            if usern and pwd:
                token = base64.b64encode(f"{usern}:{pwd}".encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
            payload = {
                "Email": (cliente.correo or "").strip(),
                "Rfc": (cliente.rfc or "").strip().upper(),
                "Name": (cliente.razon_social or cliente.nombre or "").strip(),
                "FiscalRegime": getattr(getattr(cliente, "sat_regimen_fiscal", None), "codigo", ""),
                "CfdiUse": getattr(getattr(cliente, "sat_uso_cfdi", None), "codigo", ""),
                "TaxZipCode": (cliente.codigo_postal or "").strip(),
            }
            address = {
                "Street": (cliente.direccion_fiscal or "").strip(),
                "Neighborhood": (cliente.colonia or "").strip(),
                "ZipCode": (cliente.codigo_postal or "").strip(),
                "Municipality": (cliente.ciudad or "").strip(),
                "State": (cliente.estado or "").strip(),
                "Country": "MEXICO",
            }
            address = {k: v for k, v in address.items() if v}
            if address:
                payload["Address"] = address
            payload = {k: v for k, v in payload.items() if v}
            raw_body = json.dumps(payload).encode("utf-8")
            try:
                req = Request(url, data=raw_body, headers=headers, method="POST")
                with urlopen(req, timeout=10) as resp:
                    resp.read()  # ignore body; best-effort
            except (HTTPError, URLError, TimeoutError, ValueError):
                pass
        except Exception:
            pass

    def perform_destroy(self, instance):
        instance.soft_delete()

class ProveedorViewSet(viewsets.ModelViewSet):
    queryset = Proveedor.objects.filter(activo=True)
    serializer_class = ProveedorSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        return qs.none()

    def perform_destroy(self, instance):
        instance.soft_delete()

class DireccionClienteViewSet(viewsets.ModelViewSet):
    queryset = DireccionCliente.objects.filter(activo=True)
    serializer_class = DireccionClienteSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        return qs.none()

    def perform_destroy(self, instance):
        instance.soft_delete()



