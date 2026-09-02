from django.contrib import admin
from .models import Notificacion


@admin.register(Notificacion)
class NotificacionAdmin(admin.ModelAdmin):
    list_display = ("id", "usuario", "titulo", "modulo", "tipo", "leido", "created_at")
    list_filter = ("leido", "modulo", "tipo", "created_at")
    search_fields = ("titulo", "mensaje", "usuario__username", "usuario__email")
    raw_id_fields = ("empresa", "usuario")
    readonly_fields = ("created_at", "leido_at")
    list_select_related = ("empresa", "usuario")
    ordering = ("-created_at",)
