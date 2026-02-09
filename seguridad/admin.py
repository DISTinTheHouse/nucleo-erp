from django.contrib import admin
from .models import Permiso, Rol, UsuarioRol, RolPermiso

@admin.register(Permiso)
class PermisoAdmin(admin.ModelAdmin):
    list_display = ('clave', 'nombre', 'modulo')
    list_filter = ('modulo',)
    search_fields = ('clave', 'nombre')

@admin.register(Rol)
class RolAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'empresa', 'codigo', 'estatus')
    list_filter = ('empresa', 'estatus')
    search_fields = ('nombre', 'codigo')

@admin.register(UsuarioRol)
class UsuarioRolAdmin(admin.ModelAdmin):
    list_display = ('usuario', 'rol', 'empresa')
    list_filter = ('empresa', 'rol')

@admin.register(RolPermiso)
class RolPermisoAdmin(admin.ModelAdmin):
    list_display = ('rol', 'permiso')
    list_filter = ('rol',)
