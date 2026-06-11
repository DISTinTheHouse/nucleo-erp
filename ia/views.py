import logging
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import base64
from email.message import EmailMessage
import mimetypes
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ia.models import CloudIntegration


GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar"
logger = logging.getLogger("nucleo")


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


def _http_json(url, *, method="GET", data=None, json_data=None, headers=None, timeout=20):
    payload = None
    merged_headers = dict(headers or {})
    if json_data is not None:
        payload = json.dumps(json_data).encode("utf-8")
        merged_headers.setdefault("Content-Type", "application/json")
    elif data is not None:
        payload = urllib.parse.urlencode(data).encode("utf-8")
        merged_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(url, data=payload, headers=merged_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        safe_url = (url or "").split("?", 1)[0]
        logger.warning("Drive HTTPError %s %s status=%s body=%s", method, safe_url, getattr(e, "code", ""), body[:600])
        raise
    except urllib.error.URLError as e:
        safe_url = (url or "").split("?", 1)[0]
        logger.warning("Drive URLError %s %s detail=%s", method, safe_url, str(e))
        raise


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
    redirect_uri = _drive_redirect_uri(request)
    raw_client_id = (getattr(settings, "GOOGLE_DRIVE_CLIENT_ID", "") or "").strip()
    client_id_suffix = raw_client_id[-8:] if raw_client_id else ""
    logger.info(
        "Drive OAuth connect user_id=%s host=%s redirect_uri=%s client_id_suffix=%s",
        getattr(request.user, "id", None),
        request.get_host(),
        redirect_uri,
        client_id_suffix,
    )
    params = {
        "client_id": raw_client_id,
        "redirect_uri": redirect_uri,
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
    logger.info(
        "Drive OAuth callback user_id=%s has_error=%s has_code=%s",
        getattr(request.user, "id", None),
        bool((request.GET.get("error") or "").strip()),
        bool((request.GET.get("code") or "").strip()),
    )
    oauth_error = (request.GET.get("error") or "").strip()
    if oauth_error:
        error_description = (request.GET.get("error_description") or "").strip().lower()
        logger.warning(
            "Drive OAuth error user_id=%s",
            getattr(request.user, "id", None),
        )
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
        logger.warning("Drive OAuth state mismatch user_id=%s state_ok=%s", getattr(request.user, "id", None), False)
        messages.error(request, "La autenticación con Google no es válida o expiró.")
        return redirect("drive")

    code = (request.GET.get("code") or "").strip()
    if not code:
        logger.warning("Drive OAuth missing code user_id=%s", getattr(request.user, "id", None))
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


@login_required
def correo(request):
    integration = CloudIntegration.objects.filter(user=request.user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
    if not integration or not integration.access_token:
        messages.warning(request, "Conecta tu cuenta de Google para usar Gmail.")
        return redirect("drive")

    access_token = _google_drive_refresh_token(integration)
    if not access_token:
        messages.error(request, "Tu sesión de Google expiró. Vuelve a conectarte.")
        return redirect("drive")

    emails = []
    try:
        response = _http_json(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=20&q=in:inbox",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        
        messages_list = response.get("messages", [])
        for msg in messages_list:
            msg_detail = _http_json(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            headers = {h['name']: h['value'] for h in msg_detail.get('payload', {}).get('headers', [])}
            
            # Extract name and email from "From" header (e.g. "Name <email@domain.com>" -> "Name")
            from_header = headers.get('From', '')
            from_name = from_header.split('<')[0].strip().strip('"\'') if '<' in from_header else from_header
            
            emails.append({
                'id': msg['id'],
                'snippet': msg_detail.get('snippet', ''),
                'subject': headers.get('Subject', '(Sin asunto)'),
                'from': from_name,
                'date': headers.get('Date', ''),
            })
    except urllib.error.HTTPError as e:
        if getattr(e, "code", 0) in (401, 403):
            messages.error(request, "Permisos de Gmail insuficientes. Por favor, vuelve a conectar Google y acepta todos los permisos (Drive y Gmail).")
            integration.delete()
            return redirect("drive")
        else:
            messages.error(request, "Ocurrió un error al obtener tus correos.")
            logger.exception("Error al obtener correos")

    return render(request, "ia/correo.html", {
        "emails": emails,
        "account_email": integration.account_email,
    })


def get_email_body(payload):
    body = ""
    html_body = ""
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                try:
                    body += base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                except:
                    pass
            elif part['mimeType'] == 'text/html' and 'data' in part['body']:
                try:
                    html_body += base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                except:
                    pass
            elif 'parts' in part:
                sub_body, sub_html = get_email_body(part)
                body += sub_body
                html_body += sub_html
    elif 'body' in payload and 'data' in payload['body']:
        try:
            if payload['mimeType'] == 'text/html':
                html_body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
            else:
                body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
        except:
            pass
    return body, html_body

@login_required
def correo_detail(request, msg_id):
    integration = CloudIntegration.objects.filter(user=request.user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
    if not integration or not integration.access_token:
        messages.warning(request, "Conecta tu cuenta de Google para usar Gmail.")
        return redirect("drive")

    access_token = _google_drive_refresh_token(integration)
    if not access_token:
        messages.error(request, "Tu sesión de Google expiró. Vuelve a conectarte.")
        return redirect("drive")

    try:
        msg_detail = _http_json(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        
        headers = {h['name']: h['value'] for h in msg_detail.get('payload', {}).get('headers', [])}
        from_header = headers.get('From', '')
        from_name = from_header.split('<')[0].strip().strip('"\'') if '<' in from_header else from_header
        
        body, html_body = get_email_body(msg_detail.get('payload', {}))
        
        email_data = {
            'id': msg_detail['id'],
            'subject': headers.get('Subject', '(Sin asunto)'),
            'from': from_name,
            'from_full': from_header,
            'to': headers.get('To', ''),
            'date': headers.get('Date', ''),
            'body_html': html_body if html_body else body.replace('\n', '<br>'),
        }
        
        return render(request, "ia/correo_detail.html", {
            "email": email_data,
            "account_email": integration.account_email,
        })
    except urllib.error.HTTPError as e:
        messages.error(request, "Ocurrió un error al cargar el correo.")
        return redirect("correo")

@login_required
def calendario(request):
    integration = CloudIntegration.objects.filter(user=request.user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
    if not integration or not integration.access_token:
        messages.warning(request, "Conecta tu cuenta de Google primero para usar Calendario.")
        return redirect("drive")
    
    scopes = integration.metadata.get("scope", "") if integration.metadata else ""
    if "calendar" not in scopes.lower():
        messages.warning(request, "Se requieren nuevos permisos de Calendario. Por favor, ve a la pestaña Drive, desconecta tu cuenta de Google y vuelve a conectarla para autorizar el calendario.")
        return redirect("drive")
    
    access_token = _google_drive_refresh_token(integration)
    events = []
    calendar_error = ""
    
    # Calculate timeMin for upcoming events
    now = timezone.now().isoformat()
    
    try:
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events?timeMin={urllib.parse.quote(now)}&maxResults=50&singleEvents=true&orderBy=startTime"
        response = _http_json(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            }
        )
        
        for item in response.get("items") or []:
            start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
            end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
            events.append({
                "id": item.get("id"),
                "summary": item.get("summary") or "Sin título",
                "description": item.get("description") or "",
                "htmlLink": item.get("htmlLink"),
                "start": start,
                "end": end,
                "status": item.get("status"),
                "creator": item.get("creator", {}).get("email"),
            })
    except urllib.error.HTTPError as e:
        logger.error("Calendar HTTPError: %s", str(e))
        calendar_error = "Error al consultar tu calendario. Intenta reconectar tu cuenta de Google."
    except Exception as e:
        logger.error("Calendar Exception: %s", str(e))
        calendar_error = "Error inesperado al cargar eventos."

    context = {
        "events": events,
        "calendar_error": calendar_error,
        "integration": integration,
    }
    return render(request, "ia/calendario.html", context)

@login_required
@require_POST
def calendario_create(request):
    integration = CloudIntegration.objects.filter(user=request.user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
    if not integration or not integration.access_token:
        messages.warning(request, "Conecta tu cuenta de Google primero.")
        return redirect("drive")
        
    scopes = integration.metadata.get("scope", "") if integration.metadata else ""
    if "calendar" not in scopes.lower():
        messages.warning(request, "Se requieren nuevos permisos de Calendario. Por favor, ve a la pestaña Drive, desconecta tu cuenta de Google y vuelve a conectarla.")
        return redirect("drive")
        
    access_token = _google_drive_refresh_token(integration)
    
    summary = request.POST.get("summary")
    description = request.POST.get("description", "")
    start_date = request.POST.get("start_date")
    start_time = request.POST.get("start_time")
    end_date = request.POST.get("end_date")
    end_time = request.POST.get("end_time")
    
    if not summary or not start_date or not end_date:
        messages.error(request, "Faltan campos obligatorios para el evento.")
        return redirect("calendario")
        
    try:
        # Construct ISO 8601 strings
        if start_time:
            start_dt = f"{start_date}T{start_time}:00"
            end_dt = f"{end_date}T{end_time}:00" if end_time else f"{end_date}T{start_time}:00"
            start_data = {"dateTime": start_dt, "timeZone": getattr(settings, "TIME_ZONE", "UTC")}
            end_data = {"dateTime": end_dt, "timeZone": getattr(settings, "TIME_ZONE", "UTC")}
        else:
            # All day event
            start_data = {"date": start_date}
            end_data = {"date": end_date}
            
        event_body = {
            "summary": summary,
            "description": description,
            "start": start_data,
            "end": end_data,
        }
        
        _http_json(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}"},
            json_data=event_body
        )
        messages.success(request, "Evento creado correctamente.")
    except Exception as e:
        logger.error("Error creating event: %s", str(e))
        messages.error(request, "Error al crear el evento en el calendario.")
        
    return redirect("calendario")


@login_required
@require_POST
def correo_send(request):
    integration = CloudIntegration.objects.filter(user=request.user, provider=CloudIntegration.PROVIDER_GOOGLE_DRIVE).first()
    if not integration or not integration.access_token:
        messages.warning(request, "Conecta tu cuenta de Google primero.")
        return redirect("drive")

    access_token = _google_drive_refresh_token(integration)
    
    to_email = request.POST.get("to")
    subject = request.POST.get("subject", "(Sin asunto)")
    body = request.POST.get("body", "")
    
    if not to_email or not body:
        messages.error(request, "Faltan campos obligatorios para enviar el correo.")
        return redirect("correo")
        
    try:
        message = EmailMessage()
        message.set_content(body)
        message['To'] = to_email
        message['From'] = integration.account_email
        message['Subject'] = subject
        
        for f in request.FILES.getlist('attachments'):
            file_data = f.read()
            mime_type = mimetypes.guess_type(f.name)[0]
            if mime_type:
                maintype, subtype = mime_type.split('/', 1)
            else:
                maintype, subtype = 'application', 'octet-stream'
            message.add_attachment(file_data, maintype=maintype, subtype=subtype, filename=f.name)

        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

        _http_json(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}"},
            json_data={"raw": encoded_message}
        )
        messages.success(request, "Correo enviado correctamente.")
    except Exception as e:
        logger.error("Error sending email: %s", str(e))
        messages.error(request, "Error al enviar el correo.")

    return redirect("correo")
