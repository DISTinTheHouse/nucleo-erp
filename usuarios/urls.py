from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenVerifyView

from auth_kit.app_settings import auth_kit_settings
from auth_kit.mfa.mfa_settings import auth_kit_mfa_settings
from .views import (
    UsuarioListView, UsuarioCreateView, UsuarioUpdateView, UsuarioDeleteView, UsuarioPermisosView
)
from .api.api_views import UsuarioViewSet

app_name = 'usuarios'

router = DefaultRouter()
router.register(r'usuarios', UsuarioViewSet)

login_view = (
    auth_kit_mfa_settings.LOGIN_FIRST_STEP_VIEW
    if auth_kit_settings.USE_MFA
    else auth_kit_settings.LOGIN_VIEW
)

urlpatterns = [
    # Vistas Web (Core)
    path('core/usuarios/', UsuarioListView.as_view(), name='usuario_list'),
    path('core/usuarios/nuevo/', UsuarioCreateView.as_view(), name='usuario_create'),
    path('core/usuarios/<pk>/editar/', UsuarioUpdateView.as_view(), name='usuario_update'),
    path('core/usuarios/<pk>/permisos/', UsuarioPermisosView.as_view(), name='usuario_permisos'),
    path('core/usuarios/<pk>/eliminar/', UsuarioDeleteView.as_view(), name='usuario_delete'),
    
    # API Endpoints
    path('api/v1/login/', login_view.as_view(), name='api_login'),
    path('api/v1/logout/', auth_kit_settings.LOGOUT_VIEW.as_view(), name='api_logout'),
    path('api/v1/me/', auth_kit_settings.USER_VIEW.as_view(), name='api_me'),
    path('api/v1/', include(router.urls)),
    # --
]

if auth_kit_settings.USE_MFA:
    urlpatterns += [
        path(
            'api/v1/login/verify/',
            auth_kit_mfa_settings.LOGIN_SECOND_STEP_VIEW.as_view(),
            name='api_login_verify',
        ),
        path(
            'api/v1/login/change-method/',
            auth_kit_mfa_settings.LOGIN_CHANGE_METHOD_VIEW.as_view(),
            name='api_login_change_method',
        ),
        path(
            'api/v1/login/resend/',
            auth_kit_mfa_settings.LOGIN_MFA_RESEND_VIEW.as_view(),
            name='api_login_resend',
        ),
    ]

if auth_kit_settings.AUTH_TYPE == "jwt":
    urlpatterns += [
        path(
            'api/v1/token/refresh/',
            auth_kit_settings.JWT_REFRESH_VIEW.as_view(),
            name='api_token_refresh',
        ),
        path('api/v1/token/verify/', TokenVerifyView.as_view(), name='api_token_verify'),
    ]
