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
from django.db import transaction
from django.db.utils import OperationalError, ProgrammingError
import io
import logging
import base64
import qrcode
from nucleo.mixins import AuditLogMixin
from seguridad.models import Permiso, UsuarioPermiso
from .models import Usuario
from .forms import UsuarioCreationForm, UsuarioChangeForm
from auth_kit.mfa.handlers.base import MFAHandlerRegistry
from auth_kit.mfa.models import MFAMethod
from pyotp import random_base32

logger = logging.getLogger(__name__)

def _qr_png_data_uri(value):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(value)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"

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
            return super().form_valid(form)

        self.request.session.cycle_key()

        try:
            mfa_method = MFAMethod.objects.filter(user=user, name="app").order_by("-is_active", "-is_primary", "-id").first()
            setup = (not mfa_method) or (not bool(getattr(mfa_method, "is_active", False)))
            qr_link = None
            backup_codes = None
            if setup and not mfa_method:
                with transaction.atomic():
                    MFAMethod.objects.filter(user=user, name="app").delete()
                    secret = random_base32()
                    mfa_method, raw_codes = MFAMethod.objects.create_with_backup_codes(
                        user=user,
                        name="app",
                        secret=secret,
                        is_primary=False,
                        is_active=False,
                    )
                    MFAMethod.objects.filter(user=user).exclude(pk=mfa_method.pk).update(is_primary=False)
                    backup_codes = sorted(list(raw_codes))

            if setup and mfa_method:
                handler = MFAHandlerRegistry.get_handler(mfa_method)
                qr_link = (handler.initialize_method() or {}).get("qr_link")
        except (OperationalError, ProgrammingError):
            logger.exception("MFA no disponible (error de base de datos).")
            messages.error(self.request, "MFA no disponible. Contacta al administrador.")
            return redirect("login")
        except Exception:
            logger.exception("Error inesperado en MFA durante login.")
            messages.error(self.request, "No se pudo iniciar MFA. Intenta nuevamente.")
            return redirect("login")

        self.request.session["mfa_user_id"] = user.pk
        self.request.session["mfa_backend"] = getattr(user, "backend", "") or ""
        self.request.session["mfa_next"] = self.get_success_url()
        self.request.session["mfa_method_id"] = mfa_method.pk if mfa_method else None
        self.request.session["mfa_setup"] = bool(setup)
        if qr_link:
            self.request.session["mfa_qr_link"] = qr_link
        if backup_codes:
            self.request.session["mfa_backup_codes"] = backup_codes
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
            "backup_codes": request.session.get("mfa_backup_codes") or None,
        }

    def _clear(self, request):
        for k in [
            "mfa_user_id",
            "mfa_backend",
            "mfa_next",
            "mfa_method_id",
            "mfa_setup",
            "mfa_qr_link",
            "mfa_backup_codes",
        ]:
            try:
                del request.session[k]
            except KeyError:
                pass

    def get(self, request):
        mfa_state = self._get_mfa_session_state(request)
        if not mfa_state:
            return redirect("login")

        try:
            mfa_method = MFAMethod.objects.filter(pk=mfa_state["method_id"], user_id=mfa_state["user_id"]).first()
        except (OperationalError, ProgrammingError):
            logger.exception("MFA no disponible (error de base de datos).")
            self._clear(request)
            messages.error(request, "MFA no disponible. Contacta al administrador.")
            return redirect("login")
        except Exception:
            logger.exception("Error inesperado en MFA (GET /two-factor/).")
            self._clear(request)
            messages.error(request, "Ocurrió un error al cargar MFA. Intenta nuevamente.")
            return redirect("login")

        if not mfa_method:
            self._clear(request)
            return redirect("login")

        setup = bool(mfa_state.get("setup")) or (not bool(getattr(mfa_method, "is_active", False)))
        qr_link = mfa_state.get("qr_link")
        if setup and not qr_link:
            handler = MFAHandlerRegistry.get_handler(mfa_method)
            qr_link = (handler.initialize_method() or {}).get("qr_link")
            if qr_link:
                request.session["mfa_qr_link"] = qr_link
        qr_img = _qr_png_data_uri(qr_link) if (setup and qr_link) else None
        return render(
            request,
            self.template_name,
            {
                "mode": "setup" if setup else "verify",
                "qr_img": qr_img,
            },
        )

    def post(self, request):
        mfa_state = self._get_mfa_session_state(request)
        if not mfa_state:
            return redirect("login")

        try:
            mfa_method = MFAMethod.objects.filter(pk=mfa_state["method_id"], user_id=mfa_state["user_id"]).first()
        except (OperationalError, ProgrammingError):
            logger.exception("MFA no disponible (error de base de datos).")
            self._clear(request)
            messages.error(request, "MFA no disponible. Contacta al administrador.")
            return redirect("login")
        except Exception:
            logger.exception("Error inesperado en MFA (POST /two-factor/).")
            self._clear(request)
            messages.error(request, "Ocurrió un error al validar MFA. Intenta nuevamente.")
            return redirect("login")

        if not mfa_method:
            self._clear(request)
            messages.error(request, "Usuario inválido. Inicia sesión nuevamente.")
            return redirect("login")

        if "regenerate" in request.POST:
            with transaction.atomic():
                MFAMethod.objects.filter(user_id=mfa_method.user_id, name="app").delete()
                secret = random_base32()
                mfa_method, raw_codes = MFAMethod.objects.create_with_backup_codes(
                    user_id=mfa_method.user_id,
                    name="app",
                    secret=secret,
                    is_primary=False,
                    is_active=False,
                )
                MFAMethod.objects.filter(user_id=mfa_method.user_id).exclude(pk=mfa_method.pk).update(is_primary=False)
            handler = MFAHandlerRegistry.get_handler(mfa_method)
            qr_link = (handler.initialize_method() or {}).get("qr_link")
            request.session["mfa_method_id"] = mfa_method.pk
            request.session["mfa_setup"] = True
            request.session["mfa_qr_link"] = qr_link
            request.session["mfa_backup_codes"] = sorted(list(raw_codes))
            return redirect("two_factor_verify")

        code = (request.POST.get("code") or "").strip()
        handler = MFAHandlerRegistry.get_handler(mfa_method)
        if not handler.validate_code(code):
            qr_link = mfa_state.get("qr_link")
            setup = bool(mfa_state.get("setup")) or (not bool(getattr(mfa_method, "is_active", False)))
            qr_img = _qr_png_data_uri(qr_link) if (setup and qr_link) else None
            return render(
                request,
                self.template_name,
                {
                    "mode": "setup" if setup else "verify",
                    "qr_img": qr_img,
                    "error": "Código inválido.",
                },
            )

        if mfa_state.get("setup") or not bool(getattr(mfa_method, "is_active", False)):
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
