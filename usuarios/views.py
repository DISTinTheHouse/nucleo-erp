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

logger = logging.getLogger(__name__)

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
        if not getattr(user, "two_factor_enabled", False):
            return super().form_valid(form)

        self.request.session.cycle_key()

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

# class TwoFactorVerifyView(View):
#     template_name = "registration/two_factor.html"

#     def _get_session_state(self, request):
#         user_id = request.session.get("two_factor_user_id")
#         otp_hash = request.session.get("two_factor_hash")
#         salt = request.session.get("two_factor_salt")
#         expires_at_raw = request.session.get("two_factor_expires_at")
#         if not user_id or not otp_hash or not salt or not expires_at_raw:
#             return None
#         try:
#             expires_at = timezone.datetime.fromisoformat(expires_at_raw)
#             if timezone.is_naive(expires_at):
#                 expires_at = timezone.make_aware(expires_at, timezone.get_current_timezone())
#         except Exception:
#             return None
#         return {
#             "user_id": user_id,
#             "otp_hash": otp_hash,
#             "salt": salt,
#             "expires_at": expires_at,
#         }

#     def _clear(self, request):
#         for k in [
#             "two_factor_user_id",
#             "two_factor_backend",
#             "two_factor_hash",
#             "two_factor_salt",
#             "two_factor_expires_at",
#             "two_factor_attempts",
#             "two_factor_next",
#             "two_factor_debug_code",
#         ]:
#             try:
#                 del request.session[k]
#             except KeyError:
#                 pass

#     def get(self, request):
#         state = self._get_session_state(request)
#         if not state:
#             return redirect("login")
#         if timezone.now() > state["expires_at"]:
#             self._clear(request)
#             messages.error(request, "El código expiró. Inicia sesión nuevamente.")
#             return redirect("login")
#         debug_code = request.session.get("two_factor_debug_code") if (getattr(settings, "TWO_FACTOR_DEBUG_SHOW_CODE", False) or getattr(settings, "DEBUG", False)) else None
#         return render(request, self.template_name, {"debug_code": debug_code})

#     def post(self, request):
#         state = self._get_session_state(request)
#         if not state:
#             return redirect("login")

#         if "resend" in request.POST:
#             otp_length = int(getattr(settings, "TWO_FACTOR_OTP_LENGTH", 6) or 6)
#             otp_ttl_seconds = int(getattr(settings, "TWO_FACTOR_OTP_TTL_SECONDS", 300) or 300)
#             otp_length = max(4, min(10, otp_length))
#             otp_ttl_seconds = max(60, min(3600, otp_ttl_seconds))
#             max_value = 10 ** otp_length
#             otp_code = str(secrets.randbelow(max_value)).zfill(otp_length)
#             salt = secrets.token_hex(16)
#             otp_hash = hashlib.sha256(f"{salt}{otp_code}".encode("utf-8")).hexdigest()
#             expires_at = timezone.now() + timedelta(seconds=otp_ttl_seconds)

#             request.session["two_factor_hash"] = otp_hash
#             request.session["two_factor_salt"] = salt
#             request.session["two_factor_expires_at"] = expires_at.isoformat()
#             request.session["two_factor_attempts"] = 0

#             debug_show = bool(getattr(settings, "TWO_FACTOR_DEBUG_SHOW_CODE", False))
#             debug_active = bool(debug_show)
#             if debug_active:
#                 request.session["two_factor_debug_code"] = otp_code

#             user = Usuario.objects.filter(pk=state["user_id"], is_active=True).first()
#             result = _send_two_factor_code(user, otp_code) if user else {"ok": False, "channel": None}
#             if result.get("ok"):
#                 messages.success(request, "Código reenviado.")
#             elif debug_active:
#                 messages.warning(request, "No se pudo reenviar el código automáticamente; usando modo debug.")
#             else:
#                 messages.error(request, "No se pudo reenviar el código. Revisa la configuración de SMS/Email.")
#             return redirect("two_factor_verify")

#         attempts = int(request.session.get("two_factor_attempts") or 0)
#         max_attempts = int(getattr(settings, "TWO_FACTOR_MAX_ATTEMPTS", 5) or 5)
#         if attempts >= max_attempts:
#             self._clear(request)
#             messages.error(request, "Se excedió el número de intentos. Inicia sesión nuevamente.")
#             return redirect("login")

#         if timezone.now() > state["expires_at"]:
#             self._clear(request)
#             messages.error(request, "El código expiró. Inicia sesión nuevamente.")
#             return redirect("login")

#         code = (request.POST.get("code") or "").strip()
#         candidate_hash = hashlib.sha256(f"{state['salt']}{code}".encode("utf-8")).hexdigest()
#         if not hmac.compare_digest(candidate_hash, state["otp_hash"]):
#             request.session["two_factor_attempts"] = attempts + 1
#             debug_code = request.session.get("two_factor_debug_code") if (getattr(settings, "TWO_FACTOR_DEBUG_SHOW_CODE", False) or getattr(settings, "DEBUG", False)) else None
#             return render(request, self.template_name, {"error": "Código inválido.", "debug_code": debug_code})

#         try:
#             user = Usuario.objects.get(pk=state["user_id"], is_active=True)
#         except Usuario.DoesNotExist:
#             self._clear(request)
#             messages.error(request, "Usuario inválido. Inicia sesión nuevamente.")
#             return redirect("login")

#         backend = request.session.get("two_factor_backend") or None
#         next_url = request.session.get("two_factor_next") or getattr(settings, "LOGIN_REDIRECT_URL", "/")
#         self._clear(request)
#         if backend:
#             auth_login(request, user, backend=backend)
#         else:
#             auth_login(request, user)
#         return redirect(next_url)
