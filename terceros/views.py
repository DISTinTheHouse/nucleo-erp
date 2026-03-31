from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import render
from django.contrib import messages
from django.conf import settings
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import base64
import json

class RfcStatusView(LoginRequiredMixin, View):
    template_name = "terceros/rfc_status.html"

    def get(self, request):
        base_url = getattr(settings, "FACTURAMA_BASE_URL", "https://apisandbox.facturama.mx").rstrip("/")
        is_sandbox = "apisandbox.facturama.mx" in base_url.lower()
        return render(request, self.template_name, {"result": None, "is_sandbox": is_sandbox})

    def post(self, request):
        rfc = (request.POST.get("rfc") or "").strip().upper()
        base_url = getattr(settings, "FACTURAMA_BASE_URL", "https://apisandbox.facturama.mx").rstrip("/")
        is_sandbox = "apisandbox.facturama.mx" in base_url.lower()
        url = f"{base_url}/customers/status?{urlencode({'rfc': rfc})}"
        headers = {"Accept": "application/json"}
        user = (getattr(settings, "FACTURAMA_USERNAME", "") or "").strip()
        pwd = (getattr(settings, "FACTURAMA_PASSWORD", "") or "").strip()
        if user and pwd:
            token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        data = None
        try:
            req = Request(url, headers=headers, method="GET")
            with urlopen(req, timeout=10) as resp:
                payload = resp.read().decode("utf-8")
                data = json.loads(payload) if payload else None
        except (HTTPError, URLError, TimeoutError, ValueError):
            messages.error(request, "No se pudo validar el RFC en este momento.")
        if data and isinstance(data, dict):
            ok_fmt = bool(data.get("FormatoCorrecto"))
            ok_act = bool(data.get("Activo"))
            ok_loc = bool(data.get("Localizado"))
            if ok_fmt and ok_act and ok_loc:
                messages.success(request, "RFC válido, activo y localizado en SAT.")
            elif ok_fmt:
                messages.warning(request, "El formato del RFC es correcto, pero no se reporta activo/localizado.")
            else:
                messages.error(request, "El RFC no tiene formato válido.")
        return render(request, self.template_name, {"result": data, "rfc": rfc, "is_sandbox": is_sandbox})

class ClientCreateView(LoginRequiredMixin, View):
    template_name = "terceros/client_by_id.html"

    def get(self, request):
        base_url = getattr(settings, "FACTURAMA_BASE_URL", "https://apisandbox.facturama.mx").rstrip("/")
        is_sandbox = "apisandbox.facturama.mx" in base_url.lower()
        return render(request, self.template_name, {"result": None, "is_sandbox": is_sandbox})

    def post(self, request):
        base_url = getattr(settings, "FACTURAMA_BASE_URL", "https://apisandbox.facturama.mx").rstrip("/")
        is_sandbox = "apisandbox.facturama.mx" in base_url.lower()
        url = f"{base_url}/Client"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        user = (getattr(settings, "FACTURAMA_USERNAME", "") or "").strip()
        pwd = (getattr(settings, "FACTURAMA_PASSWORD", "") or "").strip()
        if user and pwd:
            token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"

        payload = {
            "Email": (request.POST.get("email") or "").strip(),
            "EmailOp1": (request.POST.get("email_op1") or "").strip(),
            "EmailOp2": (request.POST.get("email_op2") or "").strip(),
            "Rfc": (request.POST.get("rfc") or "").strip().upper(),
            "Name": (request.POST.get("name") or "").strip(),
            "FiscalRegime": (request.POST.get("fiscal_regime") or "").strip(),
            "CfdiUse": (request.POST.get("cfdi_use") or "").strip(),
            "TaxResidence": (request.POST.get("tax_residence") or "").strip(),
            "NumRegIdTrib": (request.POST.get("num_reg_id_trib") or "").strip(),
            "TaxZipCode": (request.POST.get("tax_zip_code") or "").strip(),
        }
        required_keys = {"Email", "Rfc", "Name", "CfdiUse"}
        missing = [k for k in sorted(required_keys) if not payload.get(k)]
        if missing:
            messages.error(request, "Completa los campos requeridos: " + ", ".join(missing))
            return render(request, self.template_name, {"result": None, "is_sandbox": is_sandbox, "form": payload})
        payload = {k: v for k, v in payload.items() if v or k in required_keys}

        address = {
            "Street": (request.POST.get("address_street") or "").strip(),
            "ExteriorNumber": (request.POST.get("address_exterior_number") or "").strip(),
            "InteriorNumber": (request.POST.get("address_interior_number") or "").strip(),
            "Neighborhood": (request.POST.get("address_neighborhood") or "").strip(),
            "ZipCode": (request.POST.get("address_zip_code") or "").strip(),
            "Locality": (request.POST.get("address_locality") or "").strip(),
            "Municipality": (request.POST.get("address_municipality") or "").strip(),
            "State": (request.POST.get("address_state") or "").strip(),
            "Country": (request.POST.get("address_country") or "").strip(),
        }
        address = {k: v for k, v in address.items() if v}
        if address:
            payload["Address"] = address

        raw_body = json.dumps(payload).encode("utf-8")
        data = None
        try:
            req = Request(url, data=raw_body, headers=headers, method="POST")
            with urlopen(req, timeout=10) as resp:
                payload = resp.read().decode("utf-8")
                data = json.loads(payload) if payload else None
        except HTTPError as e:
            try:
                raw = e.read()
                payload = raw.decode("utf-8") if raw else ""
            except Exception:
                payload = ""
            if e.code in (401, 403):
                messages.error(request, "No autorizado al crear el cliente en Facturama. Revisa FACTURAMA_USERNAME/FACTURAMA_PASSWORD.")
            elif e.code == 400:
                messages.error(request, "Facturama rechazó la petición. Revisa los campos requeridos.")
            else:
                messages.error(request, f"Error {e.code} al crear el cliente.")
        except (URLError, TimeoutError):
            messages.error(request, "No se pudo conectar a Facturama en este momento.")
        except ValueError:
            messages.error(request, "Respuesta inválida al crear el cliente.")
        if data and isinstance(data, dict):
            messages.success(request, "Cliente creado en Facturama.")
        return render(request, self.template_name, {"result": data, "is_sandbox": is_sandbox, "form": payload})
