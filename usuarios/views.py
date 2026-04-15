from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import LoginView
from django.contrib.auth import login as auth_login
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib import messages
from django.db.models import Q
from django.utils import timezone
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from datetime import timedelta
import logging
import hashlib
import hmac
import secrets
import base64
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from nucleo.mixins import AuditLogMixin
from seguridad.models import Permiso, UsuarioPermiso
from .models import Usuario
from .forms import UsuarioCreationForm, UsuarioChangeForm
from auth_kit.mfa.handlers.base import MFAHandlerRegistry
from auth_kit.mfa.models import MFAMethod
from pyotp import random_base32

logger = logging.getLogger(__name__)

def _normalize_phone(raw):
    raw = str(raw or "").strip()
    if not raw:
        return ""
    allowed = set("0123456789+")
    cleaned = "".join(ch for ch in raw if ch in allowed)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    return cleaned

def _send_two_factor_code(user, code):
    telefono = _normalize_phone(getattr(user, "telefono", ""))
    sms_enabled = bool(getattr(settings, "TWO_FACTOR_SMS_ENABLED", False))
    twilio_sid = str(getattr(settings, "TWILIO_ACCOUNT_SID", "") or "").strip()
    twilio_token = str(getattr(settings, "TWILIO_AUTH_TOKEN", "") or "").strip()
    twilio_from = _normalize_phone(getattr(settings, "TWILIO_FROM_NUMBER", ""))

    if sms_enabled and telefono and twilio_sid and twilio_token and twilio_from:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{twilio_sid}/Messages.json"
        data = urlencode({"To": telefono, "From": twilio_from, "Body": f"Tu código de acceso es: {code}"}).encode("utf-8")
        auth = base64.b64encode(f"{twilio_sid}:{twilio_token}".encode("utf-8")).decode("ascii")
        req = Request(url, data=data, method="POST", headers={"Authorization": f"Basic {auth}"})
        try:
            with urlopen(req, timeout=10) as _:
                return {"ok": True, "channel": "sms"}
        except (HTTPError, URLError, TimeoutError, ValueError):
            logger.exception("Error enviando 2FA por SMS (Twilio).")

    email = str(getattr(user, "email", "") or "").strip()
    if email:
        brand = str(getattr(settings, "TWO_FACTOR_EMAIL_BRAND", "") or "").strip() or "ERP Core"
        subject = str(getattr(settings, "TWO_FACTOR_EMAIL_SUBJECT", "") or "").strip() or f"{brand} - Código de verificación"
        message = f"Tu código de acceso es: {code}"
        from_email = (
            (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip()
            or (getattr(settings, "EMAIL_HOST_USER", "") or "").strip()
            or None
        )
        try:
            html = f"""\
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#020617;font-family:Inter,Segoe UI,Roboto,Arial,sans-serif;color:#e2e8f0;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#020617;padding:28px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;">
          <tr>
            <td align="center" style="padding:10px 0 18px 0;">
              <table role="presentation" cellspacing="0" cellpadding="0">
                <tr>
                  <td align="center" style="padding:0 0 10px 0;">
                    <table role="presentation" cellspacing="0" cellpadding="0" style="border-collapse:separate;border-spacing:8px 8px;">
                      <tr>
                        <td></td>
                        <td align="center">
                          <span style="display:inline-block;width:10px;height:10px;border-radius:999px;background:#38bdf8;opacity:0.8;"></span>
                        </td>
                        <td></td>
                      </tr>
                      <tr>
                        <td align="center">
                          <span style="display:inline-block;width:10px;height:10px;border-radius:999px;background:#38bdf8;opacity:0.8;"></span>
                        </td>
                        <td></td>
                        <td align="center">
                          <span style="display:inline-block;width:10px;height:10px;border-radius:999px;background:#38bdf8;opacity:0.8;"></span>
                        </td>
                      </tr>
                      <tr>
                        <td></td>
                        <td align="center">
                          <span style="display:inline-block;width:10px;height:10px;border-radius:999px;background:#38bdf8;opacity:0.8;"></span>
                        </td>
                        <td></td>
                      </tr>
                    </table>
                  </td>
                </tr>
                <tr>
                  <td align="center" style="font-size:30px;line-height:36px;font-weight:800;color:#ffffff;padding:0 0 6px 0;">
                    {brand}
                  </td>
                </tr>
                <tr>
                  <td align="center" style="font-size:13px;line-height:18px;color:#94a3b8;">
                    Acceso Administrativo
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td align="center">
              <table role="presentation" width="560" cellspacing="0" cellpadding="0" style="max-width:560px;width:100%;background:#0b1220;border:1px solid rgba(148,163,184,0.22);border-radius:18px;overflow:hidden;">
                <tr>
                  <td style="padding:22px 22px 6px 22px;">
                    <div style="font-size:18px;line-height:26px;font-weight:800;color:#ffffff;margin:0 0 8px 0;">Verificación</div>
                    <div style="font-size:13px;line-height:18px;color:#94a3b8;margin:0 0 14px 0;">Ingresa este código para completar el acceso</div>
                    <div style="text-align:center;margin:18px 0 14px 0;">
                      <div style="display:inline-block;padding:14px 18px;border-radius:14px;border:1px solid rgba(148,163,184,0.22);background:rgba(2,6,23,0.55);">
                        <span style="font-size:30px;letter-spacing:6px;font-weight:900;color:#ffffff;">{code}</span>
                      </div>
                    </div>
                    <div style="font-size:12px;line-height:18px;color:#94a3b8;margin:0 0 18px 0;">
                      Si no solicitaste este código, ignora este correo.
                    </div>
                  </td>
                </tr>
                <tr>
                  <td style="padding:0 22px 22px 22px;">
                    <div style="display:block;background:#0ea5e9;background-image:linear-gradient(90deg,#0ea5e9,#2563eb);border-radius:14px;text-align:center;">
                      <span style="display:block;padding:12px 14px;color:#ffffff;font-weight:800;font-size:14px;letter-spacing:0.2px;">
                        Código válido por pocos minutos
                      </span>
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:18px 0 0 0;">
              <div style="font-size:12px;line-height:18px;color:#475569;">
                &copy; 2026 ERP System. Todos los derechos reservados.
              </div>
              <div style="font-size:12px;line-height:18px;color:#475569;margin-top:6px;">
                Este es un mensaje automático. No respondas a este correo.
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
            email_msg = EmailMultiAlternatives(subject=subject, body=message, from_email=from_email, to=[email])
            email_msg.attach_alternative(html, "text/html")
            sent_count = email_msg.send(fail_silently=False)
            if sent_count:
                return {"ok": True, "channel": "email"}
        except Exception:
            logger.exception("Error enviando 2FA por correo (SMTP).")

    return {"ok": False, "channel": None}

class SuperuserRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_superuser

class UsuarioListView(LoginRequiredMixin, SuperuserRequiredMixin, ListView):
    model = Usuario
    template_name = 'usuarios/usuario_list.html'
    context_object_name = 'usuarios'

    def get_queryset(self):
        qs = (
            Usuario.objects.select_related("empresa").prefetch_related("asignaciones_roles__rol")
            .all()
            .order_by("empresa__codigo", "username")
        )
        q = self.request.GET.get('q')
        if q:
            qs = qs.filter(
                Q(username__icontains=q) | 
                Q(first_name__icontains=q) | 
                Q(last_name__icontains=q) | 
                Q(email__icontains=q) |
                Q(empresa__razon_social__icontains=q)
            )
        return qs

class UsuarioCreateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, CreateView):
    model = Usuario
    form_class = UsuarioCreationForm
    template_name = 'usuarios/usuario_form.html'
    success_url = reverse_lazy('usuarios:usuario_list')
    success_message = "El usuario %(username)s fue creado correctamente."

class UsuarioUpdateView(AuditLogMixin, SuccessMessageMixin, LoginRequiredMixin, SuperuserRequiredMixin, UpdateView):
    model = Usuario
    form_class = UsuarioChangeForm
    template_name = 'usuarios/usuario_form.html'
    success_url = reverse_lazy('usuarios:usuario_list')
    success_message = "El usuario %(username)s fue editado correctamente."

class UsuarioDeleteView(AuditLogMixin, LoginRequiredMixin, SuperuserRequiredMixin, DeleteView):
    model = Usuario
    template_name = 'usuarios/usuario_confirm_delete.html'
    success_url = reverse_lazy('usuarios:usuario_list')
    success_message = "El usuario %(username)s fue eliminado correctamente."

class UsuarioPermisosView(LoginRequiredMixin, SuperuserRequiredMixin, View):
    template_name = 'usuarios/usuario_permisos.html'

    def get(self, request, pk):
        usuario = get_object_or_404(Usuario, pk=pk)
        
        permisos = Permiso.objects.all().order_by('modulo', 'clave')
        
        permisos_by_modulo = {}
        for p in permisos:
            mod = p.modulo if p.modulo else 'General'
            if mod not in permisos_by_modulo:
                permisos_by_modulo[mod] = []
            permisos_by_modulo[mod].append(p)
            
        overrides = {
            up.permiso_id: up.tipo 
            for up in usuario.overrides_permisos.all()
        }
        
        context = {
            'usuario': usuario,
            'permisos_by_modulo': permisos_by_modulo,
            'overrides': overrides,
            'GRANT': UsuarioPermiso.TIPO_GRANT,
            'DENY': UsuarioPermiso.TIPO_DENY,
        }
        return render(request, self.template_name, context)

    def post(self, request, pk):
        usuario = get_object_or_404(Usuario, pk=pk)
        
        # Contexto de empresa
        empresa_ctx = usuario.empresa
        if not empresa_ctx and hasattr(request.user, 'empresa'):
            empresa_ctx = request.user.empresa

        # Procesar formulario
        permisos = Permiso.objects.all()
        
        for permiso in permisos:
            field_name = f'permiso_{permiso.id}'
            valor = request.POST.get(field_name)
            
            if not valor:
                continue
                
            override = UsuarioPermiso.objects.filter(usuario=usuario, permiso=permiso).first()
            
            if valor == 'default':
                if override:
                    override.delete()
            elif valor in [UsuarioPermiso.TIPO_GRANT, UsuarioPermiso.TIPO_DENY]:
                if override:
                    if override.tipo != valor:
                        override.tipo = valor
                        override.save()
                else:
                    UsuarioPermiso.objects.create(
                        usuario=usuario,
                        permiso=permiso,
                        tipo=valor,
                        empresa=empresa_ctx
                    )
        
        messages.success(request, f"Permisos actualizados para {usuario.username}")
        return redirect('usuarios:usuario_permisos', pk=pk)
    success_url = reverse_lazy('usuarios:usuario_list')

class TwoFactorLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        user = form.get_user()
        require_mfa = bool(getattr(settings, "AUTH_KIT", {}).get("USE_MFA"))
        require_mfa = require_mfa and bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
        if not require_mfa:
            if not getattr(user, "two_factor_enabled", False):
                return super().form_valid(form)

        self.request.session.cycle_key()

        if require_mfa:
            mfa_method = MFAMethod.objects.filter(user=user, is_primary=True, is_active=True).first()
            setup = False
            qr_link = None
            secret = None
            backup_codes = None
            if not mfa_method:
                setup = True
                with transaction.atomic():
                    MFAMethod.objects.filter(user=user, name="app").delete()
                    secret = random_base32()
                    mfa_method, raw_codes = MFAMethod.objects.create_with_backup_codes(
                        user=user,
                        name="app",
                        secret=secret,
                        is_primary=True,
                        is_active=False,
                    )
                    MFAMethod.objects.filter(user=user).exclude(pk=mfa_method.pk).update(is_primary=False)
                    backup_codes = sorted(list(raw_codes))
                handler = MFAHandlerRegistry.get_handler(mfa_method)
                qr_link = (handler.initialize_method() or {}).get("qr_link")
                if not qr_link:
                    qr_link = handler.initialize_method().get("qr_link")

            self.request.session["mfa_user_id"] = user.pk
            self.request.session["mfa_backend"] = getattr(user, "backend", "") or ""
            self.request.session["mfa_next"] = self.get_success_url()
            self.request.session["mfa_method_id"] = mfa_method.pk if mfa_method else None
            self.request.session["mfa_setup"] = bool(setup)
            if qr_link:
                self.request.session["mfa_qr_link"] = qr_link
            if secret:
                self.request.session["mfa_secret"] = secret
            if backup_codes:
                self.request.session["mfa_backup_codes"] = backup_codes
            return redirect("two_factor_verify")

        otp_length = int(getattr(settings, "TWO_FACTOR_OTP_LENGTH", 6) or 6)
        otp_ttl_seconds = int(getattr(settings, "TWO_FACTOR_OTP_TTL_SECONDS", 300) or 300)
        otp_length = max(4, min(10, otp_length))
        otp_ttl_seconds = max(60, min(3600, otp_ttl_seconds))

        max_value = 10 ** otp_length
        otp_code = str(secrets.randbelow(max_value)).zfill(otp_length)
        salt = secrets.token_hex(16)
        otp_hash = hashlib.sha256(f"{salt}{otp_code}".encode("utf-8")).hexdigest()
        expires_at = timezone.now() + timedelta(seconds=otp_ttl_seconds)

        self.request.session["two_factor_user_id"] = user.pk
        self.request.session["two_factor_backend"] = getattr(user, "backend", "") or ""
        self.request.session["two_factor_hash"] = otp_hash
        self.request.session["two_factor_salt"] = salt
        self.request.session["two_factor_expires_at"] = expires_at.isoformat()
        self.request.session["two_factor_attempts"] = 0
        self.request.session["two_factor_next"] = self.get_success_url()

        debug_show = bool(getattr(settings, "TWO_FACTOR_DEBUG_SHOW_CODE", False))
        debug_active = bool(debug_show)
        if debug_active:
            self.request.session["two_factor_debug_code"] = otp_code

        result = _send_two_factor_code(user, otp_code)
        destino = None
        if result.get("channel") == "sms":
            telefono = (getattr(user, "telefono", "") or "").strip()
            if telefono:
                destino = f"SMS al teléfono terminado en {telefono[-4:]}"
        elif result.get("channel") == "email":
            email = str(getattr(user, "email", "") or "").strip()
            if email and "@" in email:
                local, domain = email.split("@", 1)
                masked_local = (local[:2] + "***") if len(local) >= 2 else "***"
                destino = f"correo {masked_local}@{domain}"

        if result.get("ok") and destino:
            messages.success(self.request, f"Se envió un código de verificación vía {destino}.")
        elif result.get("ok"):
            messages.success(self.request, "Se envió un código de verificación.")
        elif debug_active:
            messages.warning(self.request, "No se pudo enviar el código automáticamente; usando modo debug.")
        else:
            messages.error(self.request, "No se pudo enviar el código. Revisa la configuración de SMS/Email.")

        return redirect("two_factor_verify")

class TwoFactorVerifyView(View):
    template_name = "registration/two_factor.html"

    def _get_mfa_session_state(self, request):
        user_id = request.session.get("mfa_user_id")
        method_id = request.session.get("mfa_method_id")
        if not user_id or not method_id:
            return None
        return {
            "user_id": user_id,
            "method_id": method_id,
            "setup": bool(request.session.get("mfa_setup")),
            "backend": request.session.get("mfa_backend") or None,
            "next": request.session.get("mfa_next") or getattr(settings, "LOGIN_REDIRECT_URL", "/"),
            "qr_link": request.session.get("mfa_qr_link"),
            "secret": request.session.get("mfa_secret"),
            "backup_codes": request.session.get("mfa_backup_codes") or None,
        }

    def _get_session_state(self, request):
        user_id = request.session.get("two_factor_user_id")
        otp_hash = request.session.get("two_factor_hash")
        salt = request.session.get("two_factor_salt")
        expires_at_raw = request.session.get("two_factor_expires_at")
        if not user_id or not otp_hash or not salt or not expires_at_raw:
            return None
        try:
            expires_at = timezone.datetime.fromisoformat(expires_at_raw)
            if timezone.is_naive(expires_at):
                expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
        except Exception:
            return None
        return {
            "user_id": user_id,
            "otp_hash": otp_hash,
            "salt": salt,
            "expires_at": expires_at,
        }

    def _clear(self, request):
        for k in [
            "mfa_user_id",
            "mfa_backend",
            "mfa_next",
            "mfa_method_id",
            "mfa_setup",
            "mfa_qr_link",
            "mfa_secret",
            "mfa_backup_codes",
            "two_factor_user_id",
            "two_factor_backend",
            "two_factor_hash",
            "two_factor_salt",
            "two_factor_expires_at",
            "two_factor_attempts",
            "two_factor_next",
            "two_factor_debug_code",
        ]:
            try:
                del request.session[k]
            except KeyError:
                pass

    def get(self, request):
        mfa_state = self._get_mfa_session_state(request)
        if mfa_state:
            mfa_method = MFAMethod.objects.filter(pk=mfa_state["method_id"], user_id=mfa_state["user_id"]).first()
            if not mfa_method:
                self._clear(request)
                return redirect("login")
            qr_link = mfa_state.get("qr_link")
            if mfa_state.get("setup") and not qr_link:
                handler = MFAHandlerRegistry.get_handler(mfa_method)
                qr_link = (handler.initialize_method() or {}).get("qr_link")
            secret = mfa_state.get("secret") or getattr(mfa_method, "secret", None)
            return render(
                request,
                self.template_name,
                {
                    "mode": "setup" if mfa_state.get("setup") else "verify",
                    "qr_link": qr_link,
                    "secret": secret,
                },
            )

        state = self._get_session_state(request)
        if not state:
            return redirect("login")
        if timezone.now() > state["expires_at"]:
            self._clear(request)
            messages.error(request, "El código expiró. Inicia sesión nuevamente.")
            return redirect("login")
        debug_code = request.session.get("two_factor_debug_code") if (getattr(settings, "TWO_FACTOR_DEBUG_SHOW_CODE", False) or getattr(settings, "DEBUG", False)) else None
        return render(request, self.template_name, {"mode": "legacy", "debug_code": debug_code})

    def post(self, request):
        mfa_state = self._get_mfa_session_state(request)
        if mfa_state:
            mfa_method = MFAMethod.objects.filter(pk=mfa_state["method_id"], user_id=mfa_state["user_id"]).first()
            if not mfa_method:
                self._clear(request)
                messages.error(request, "Usuario inválido. Inicia sesión nuevamente.")
                return redirect("login")

            if "regenerate" in request.POST and mfa_state.get("setup"):
                with transaction.atomic():
                    MFAMethod.objects.filter(user_id=mfa_method.user_id, name="app").delete()
                    secret = random_base32()
                    mfa_method, raw_codes = MFAMethod.objects.create_with_backup_codes(
                        user_id=mfa_method.user_id,
                        name="app",
                        secret=secret,
                        is_primary=True,
                        is_active=False,
                    )
                    MFAMethod.objects.filter(user_id=mfa_method.user_id).exclude(pk=mfa_method.pk).update(is_primary=False)
                handler = MFAHandlerRegistry.get_handler(mfa_method)
                qr_link = (handler.initialize_method() or {}).get("qr_link")
                request.session["mfa_method_id"] = mfa_method.pk
                request.session["mfa_qr_link"] = qr_link
                request.session["mfa_secret"] = mfa_method.secret
                request.session["mfa_backup_codes"] = sorted(list(raw_codes))
                return redirect("two_factor_verify")

            code = (request.POST.get("code") or "").strip()
            handler = MFAHandlerRegistry.get_handler(mfa_method)
            if not handler.validate_code(code):
                return render(
                    request,
                    self.template_name,
                    {
                        "mode": "setup" if mfa_state.get("setup") else "verify",
                        "qr_link": mfa_state.get("qr_link"),
                        "secret": mfa_state.get("secret") or getattr(mfa_method, "secret", None),
                        "error": "Código inválido.",
                    },
                )

            if mfa_state.get("setup"):
                with transaction.atomic():
                    MFAMethod.objects.filter(user_id=mfa_method.user_id).exclude(pk=mfa_method.pk).update(is_primary=False)
                    MFAMethod.objects.filter(pk=mfa_method.pk).update(is_active=True, is_primary=True)
                messages.success(request, "Autenticación configurada correctamente.")

            backend = mfa_state.get("backend")
            next_url = mfa_state.get("next")
            self._clear(request)
            user = Usuario.objects.filter(pk=mfa_state["user_id"], is_active=True).first()
            if not user:
                messages.error(request, "Usuario inválido. Inicia sesión nuevamente.")
                return redirect("login")
            if backend:
                auth_login(request, user, backend=backend)
            else:
                auth_login(request, user)
            return redirect(next_url)

        state = self._get_session_state(request)
        if not state:
            return redirect("login")

        if "resend" in request.POST:
            otp_length = int(getattr(settings, "TWO_FACTOR_OTP_LENGTH", 6) or 6)
            otp_ttl_seconds = int(getattr(settings, "TWO_FACTOR_OTP_TTL_SECONDS", 300) or 300)
            otp_length = max(4, min(10, otp_length))
            otp_ttl_seconds = max(60, min(3600, otp_ttl_seconds))
            max_value = 10 ** otp_length
            otp_code = str(secrets.randbelow(max_value)).zfill(otp_length)
            salt = secrets.token_hex(16)
            otp_hash = hashlib.sha256(f"{salt}{otp_code}".encode("utf-8")).hexdigest()
            expires_at = timezone.now() + timedelta(seconds=otp_ttl_seconds)

            request.session["two_factor_hash"] = otp_hash
            request.session["two_factor_salt"] = salt
            request.session["two_factor_expires_at"] = expires_at.isoformat()
            request.session["two_factor_attempts"] = 0

            debug_show = bool(getattr(settings, "TWO_FACTOR_DEBUG_SHOW_CODE", False))
            debug_active = bool(debug_show)
            if debug_active:
                request.session["two_factor_debug_code"] = otp_code

            user = Usuario.objects.filter(pk=state["user_id"], is_active=True).first()
            result = _send_two_factor_code(user, otp_code) if user else {"ok": False, "channel": None}
            if result.get("ok"):
                messages.success(request, "Código reenviado.")
            elif debug_active:
                messages.warning(request, "No se pudo reenviar el código automáticamente; usando modo debug.")
            else:
                messages.error(request, "No se pudo reenviar el código. Revisa la configuración de SMS/Email.")
            return redirect("two_factor_verify")

        attempts = int(request.session.get("two_factor_attempts") or 0)
        max_attempts = int(getattr(settings, "TWO_FACTOR_MAX_ATTEMPTS", 5) or 5)
        if attempts >= max_attempts:
            self._clear(request)
            messages.error(request, "Se excedió el número de intentos. Inicia sesión nuevamente.")
            return redirect("login")

        if timezone.now() > state["expires_at"]:
            self._clear(request)
            messages.error(request, "El código expiró. Inicia sesión nuevamente.")
            return redirect("login")

        code = (request.POST.get("code") or "").strip()
        candidate_hash = hashlib.sha256(f"{state['salt']}{code}".encode("utf-8")).hexdigest()
        if not hmac.compare_digest(candidate_hash, state["otp_hash"]):
            request.session["two_factor_attempts"] = attempts + 1
            debug_code = request.session.get("two_factor_debug_code") if (getattr(settings, "TWO_FACTOR_DEBUG_SHOW_CODE", False) or getattr(settings, "DEBUG", False)) else None
            return render(request, self.template_name, {"mode": "legacy", "error": "Código inválido.", "debug_code": debug_code})

        try:
            user = Usuario.objects.get(pk=state["user_id"], is_active=True)
        except Usuario.DoesNotExist:
            self._clear(request)
            messages.error(request, "Usuario inválido. Inicia sesión nuevamente.")
            return redirect("login")

        backend = request.session.get("two_factor_backend") or None
        next_url = request.session.get("two_factor_next") or getattr(settings, "LOGIN_REDIRECT_URL", "/")
        self._clear(request)
        if backend:
            auth_login(request, user, backend=backend)
        else:
            auth_login(request, user)
        return redirect(next_url)
