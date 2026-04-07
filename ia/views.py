import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ia.models import CloudIntegration


GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/userinfo.email"


def _provider_cards():
    return [
        {
            "key": CloudIntegration.PROVIDER_GOOGLE_DRIVE,
            "name": "Google Drive",
            "subtitle": "Explora carpetas y archivos de tu cuenta",
            "enabled": True,
            "badge": "Disponible",
            "icon": "G",
            "color": "from-sky-500 to-blue-600",
        },
        {
            "key": CloudIntegration.PROVIDER_DROPBOX,
            "name": "Dropbox",
            "subtitle": "Próximamente",
            "enabled": False,
            "badge": "Próximamente",
            "icon": "D",
            "color": "from-blue-500 to-indigo-600",
        },
        {
            "key": CloudIntegration.PROVIDER_ONEDRIVE,
            "name": "OneDrive",
            "subtitle": "Próximamente",
            "enabled": False,
            "badge": "Próximamente",
            "icon": "O",
            "color": "from-cyan-500 to-sky-600",
        },
    ]


def _drive_redirect_uri(request):
    configured = (getattr(settings, "GOOGLE_DRIVE_REDIRECT_URI", "") or "").strip()
    if configured:
        return configured
    return request.build_absolute_uri(reverse("drive_google_callback"))


def _google_drive_credentials_configured():
    client_id = (getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "") or "").strip()
    client_secret = (getattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "") or "").strip()
    return bool(client_id and client_secret)


def _http_json(url, *, method="GET", data=None, headers=None, timeout=20):
    payload = None
    merged_headers = dict(headers or {})
    if data is not None:
        payload = urllib.parse.urlencode(data).encode("utf-8")
        merged_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=payload, headers=merged_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _google_drive_refresh_token(integration):
    if not integration.refresh_token:
        return integration.access_token or ""

    expires_at = getattr(integration, "token_expires_at", None)
    if integration.access_token and expires_at and expires_at > timezone.now() + timedelta(minutes=2):
        return integration.access_token

    token_data = _http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        data={
            "client_id": (getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "") or "").strip(),
            "client_secret": (getattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "") or "").strip(),
            "refresh_token": integration.refresh_token,
            "grant_type": "refresh_token",
        },
    )
    integration.access_token = token_data.get("access_token", "") or integration.access_token
    expires_in = int(token_data.get("expires_in") or 3600)
    integration.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
    integration.save(update_fields=["access_token", "token_expires_at", "updated_at"])
    return integration.access_token


def _list_google_drive_files(integration, search_query=""):
    access_token = _google_drive_refresh_token(integration)
    params = {
        "pageSize": 50,
        "fields": "files(id,name,mimeType,webViewLink,webContentLink,modifiedTime,size,iconLink)",
        "orderBy": "modifiedTime desc",
    }
    if search_query:
        escaped = str(search_query).replace("\\", "\\\\").replace("'", "\\'")
        params["q"] = f"name contains '{escaped}' and trashed = false"
    else:
        params["q"] = "trashed = false"
    url = f"https://www.googleapis.com/drive/v3/files?{urllib.parse.urlencode(params)}"
    response = _http_json(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    files = []
    for item in response.get("files") or []:
        files.append(
            {
                "id": item.get("id"),
                "name": item.get("name") or "Sin nombre",
                "mime_type": item.get("mimeType") or "",
                "is_folder": item.get("mimeType") == "application/vnd.google-apps.folder",
                "web_view_link": item.get("webViewLink"),
                "web_content_link": item.get("webContentLink"),
                "modified_time": item.get("modifiedTime"),
                "size": item.get("size"),
                "icon_link": item.get("iconLink"),
            }
        )
    return files


@login_required
def creator(request):
    return render(request, "ia/creator.html")


@login_required
def drive(request):
    integrations = {
        integration.provider: integration
        for integration in CloudIntegration.objects.filter(user=request.user).order_by("provider")
    }
    active_provider_key = next(iter(integrations.keys()), None)
    google_integration = integrations.get(CloudIntegration.PROVIDER_GOOGLE_DRIVE)
    search_query = (request.GET.get("q") or "").strip()
    files = []
    drive_error = ""

    if google_integration:
        try:
            files = _list_google_drive_files(google_integration, search_query=search_query)
        except urllib.error.HTTPError:
            drive_error = "No pude consultar Google Drive. Reconecta tu cuenta e intenta de nuevo."
        except Exception:
            drive_error = "No pude cargar tus archivos en este momento."

    cards = []
    for card in _provider_cards():
        connected = card["key"] in integrations
        locked_by_existing = bool(active_provider_key and active_provider_key != card["key"])
        cards.append(
            {
                **card,
                "connected": connected,
                "locked_by_existing": locked_by_existing,
                "account_email": getattr(integrations.get(card["key"]), "account_email", ""),
            }
        )

    context = {
        "provider_cards": cards,
        "google_integration": google_integration,
        "files": files,
        "search_query": search_query,
        "drive_error": drive_error,
        "show_onboarding": request.GET.get("onboarding") == "1" or not integrations,
        "google_drive_ready": _google_drive_credentials_configured(),
        "active_provider_key": active_provider_key,
    }
    return render(request, "ia/drive.html", context)


@login_required
def drive_google_connect(request):
    if not _google_drive_credentials_configured():
        messages.error(request, "Falta configurar GOOGLE_DRIVE_CLIENT_ID y GOOGLE_DRIVE_CLIENT_SECRET.")
        return redirect("drive")

    existing = CloudIntegration.objects.filter(user=request.user).exclude(provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
    if existing:
        messages.error(request, "Ya tienes una nube conectada. Desconéctala antes de elegir otra.")
        return redirect("drive")

    state = secrets.token_urlsafe(24)
    request.session["google_drive_oauth_state"] = state
    params = {
        "client_id": (getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "") or "").strip(),
        "redirect_uri": _drive_redirect_uri(request),
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "scope": GOOGLE_DRIVE_SCOPE,
        "state": state,
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    return redirect(url)


@login_required
def drive_google_callback(request):
    oauth_error = (request.GET.get("error") or "").strip()
    if oauth_error:
        error_description = (request.GET.get("error_description") or "").strip().lower()
        if oauth_error == "access_denied":
            if "verification" in error_description or "not verified" in error_description or "developer verification" in error_description:
                messages.error(
                    request,
                    "Google bloqueó el acceso porque la app no está verificada. Agrega tu correo como Test user en el OAuth consent screen o publica la app.",
                )
            else:
                messages.error(request, "Cancelaste la conexión con Google Drive.")
        else:
            messages.error(request, "No se pudo completar la autenticación con Google.")
        request.session.pop("google_drive_oauth_state", None)
        return redirect("drive")

    state = (request.GET.get("state") or "").strip()
    expected_state = request.session.pop("google_drive_oauth_state", "")
    if not state or state != expected_state:
        messages.error(request, "La autenticación con Google no es válida o expiró.")
        return redirect("drive")

    code = (request.GET.get("code") or "").strip()
    if not code:
        messages.error(request, "Google no devolvió un código de autorización.")
        return redirect("drive")

    existing = CloudIntegration.objects.filter(user=request.user).exclude(provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
    if existing:
        messages.error(request, "Ya tienes una nube conectada. Desconéctala antes de elegir otra.")
        return redirect("drive")

    try:
        token_data = _http_json(
            "https://oauth2.googleapis.com/token",
            method="POST",
            data={
                "client_id": (getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "") or "").strip(),
                "client_secret": (getattr(settings, "GOOGLE_DRIVE_CLIENT_SECRET", "") or "").strip(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _drive_redirect_uri(request),
            },
        )
        if not (token_data.get("access_token") or "").strip():
            messages.error(request, "Google no devolvió un token de acceso. Intenta de nuevo.")
            return redirect("drive")
        user_info = _http_json(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={
                "Authorization": f"Bearer {token_data.get('access_token', '')}",
                "Accept": "application/json",
            },
        )
    except urllib.error.HTTPError:
        messages.error(request, "No pude completar la conexión con Google Drive.")
        return redirect("drive")
    except Exception:
        messages.error(request, "Ocurrió un problema al enlazar Google Drive.")
        return redirect("drive")

    integration, _ = CloudIntegration.objects.get_or_create(
        user=request.user,
        provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE,
    )
    integration.account_email = user_info.get("email", "") or integration.account_email
    integration.access_token = token_data.get("access_token", "") or integration.access_token
    incoming_refresh_token = token_data.get("refresh_token", "") or ""
    if incoming_refresh_token:
        integration.refresh_token = incoming_refresh_token
    expires_in = int(token_data.get("expires_in") or 3600)
    integration.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
    integration.metadata = {
        **(integration.metadata or {}),
        "scope": token_data.get("scope", ""),
        "token_type": token_data.get("token_type", ""),
    }
    integration.save()
    messages.success(request, "Google Drive quedó conectado correctamente.")
    return redirect(f"{reverse('drive')}?connected=1")


@login_required
@require_POST
def drive_disconnect(request, provider):
    CloudIntegration.objects.filter(user=request.user, provider=provider).delete()
    messages.success(request, "La integración se desconectó correctamente.")
    return redirect(f"{reverse('drive')}?onboarding=1")
