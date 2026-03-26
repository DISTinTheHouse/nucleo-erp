from django.contrib import admin
from .models import Permiso, Rol, UsuarioRol, RolPermiso

@admin.register(Permiso)
class PermisoAdmin(admin.ModelAdmin):
    list_display = ("clave", "nombre", "modulo", "created_at")
    list_filter = ("modulo",)
    search_fields = ("clave", "nombre", "descripcion", "modulo")
    date_hierarchy = "created_at"
    ordering = ("modulo", "clave", "id")

@admin.register(Rol)
class RolAdmin(admin.ModelAdmin):
    list_display = ("nombre", "empresa", "codigo", "estatus", "is_system", "clave_departamento", "created_at")
    list_filter = ("empresa", "estatus", "is_system", "clave_departamento")
    search_fields = ("nombre", "codigo", "descripcion", "empresa__codigo", "empresa__razon_social")
    date_hierarchy = "created_at"
    ordering = ("empresa", "nombre", "id")
    autocomplete_fields = ("empresa",)
    list_select_related = ("empresa",)

@admin.register(UsuarioRol)
class UsuarioRolAdmin(admin.ModelAdmin):
    list_display = ("usuario", "rol", "empresa", "sucursal", "created_at")
    list_filter = ("empresa", "rol", "sucursal")
    search_fields = ("usuario__username", "usuario__email", "rol__nombre", "rol__codigo", "empresa__codigo", "empresa__razon_social")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "id")
    autocomplete_fields = ("usuario", "rol", "empresa", "sucursal")
    list_select_related = ("usuario", "rol", "empresa", "sucursal")

@admin.register(RolPermiso)
class RolPermisoAdmin(admin.ModelAdmin):
    list_display = ("rol", "permiso", "created_at")
    list_filter = ("rol", "permiso", "rol__empresa")
    search_fields = ("rol__nombre", "rol__codigo", "permiso__clave", "permiso__nombre")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "id")
    autocomplete_fields = ("rol", "permiso")
    list_select_related = ("rol", "permiso")
