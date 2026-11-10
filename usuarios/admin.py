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

    def get_readonly_fields(self, request, obj=None):
        # La empresa activa propia solo cambia por ``cambiar-empresa`` (EC-843),
        # que rehace el alcance por sucursal y lo audita. A otros usuarios se les
        # sigue editando aquí.
        campos = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.pk == request.user.pk:
            campos.append("empresa")
        return campos
