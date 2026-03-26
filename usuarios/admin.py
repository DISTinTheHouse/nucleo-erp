from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Usuario

@admin.register(Usuario)
class UsuarioAdmin(UserAdmin):
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "empresa",
        "sucursal_default",
        "estatus",
        "is_admin_empresa",
        "is_staff",
        "is_superuser",
        "is_active",
    )
    list_filter = ("empresa", "estatus", "is_admin_empresa", "is_staff", "is_superuser", "is_active", "two_factor_enabled")
    search_fields = ("username", "email", "first_name", "last_name", "telefono")
    ordering = ("username",)
    autocomplete_fields = ("empresa", "sucursal_default")
    list_select_related = ("empresa", "sucursal_default")
    fieldsets = UserAdmin.fieldsets + (
        ("Información Extra", {"fields": ("empresa", "empresas", "sucursal_default", "sucursales", "departamentos", "telefono", "is_admin_empresa", "estatus", "two_factor_enabled")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Información Extra", {"fields": ("empresa", "sucursal_default", "email", "first_name", "last_name", "telefono")}),
    )
    filter_horizontal = ("empresas", "sucursales", "departamentos")
