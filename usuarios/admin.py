from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Usuario

@admin.register(Usuario)
class UsuarioAdmin(UserAdmin):
    list_display = ('username', 'email', 'first_name', 'last_name', 'empresa', 'estatus')
    list_filter = ('empresa', 'estatus', 'is_staff', 'is_superuser')
    fieldsets = UserAdmin.fieldsets + (
        ('Información Extra', {'fields': ('empresa', 'sucursal_default', 'telefono', 'is_admin_empresa', 'estatus')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Información Extra', {'fields': ('empresa', 'sucursal_default', 'email', 'first_name', 'last_name', 'telefono')}),
    )
