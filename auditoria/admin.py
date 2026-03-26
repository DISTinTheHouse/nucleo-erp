from django.contrib import admin
from auditoria.models import AuditoriaEvento


@admin.register(AuditoriaEvento)
class AuditoriaEventoAdmin(admin.ModelAdmin):
    list_display = ("id_evento", "empresa", "usuario", "modulo", "accion", "tabla", "id_registro", "ip", "created_at")
    list_filter = ("empresa", "modulo", "accion", "tabla", "created_at")
    search_fields = (
        "id_evento",
        "empresa__codigo",
        "empresa__razon_social",
        "usuario__username",
        "usuario__email",
        "modulo",
        "accion",
        "tabla",
        "id_registro",
        "ip",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id_evento")
    autocomplete_fields = ("empresa", "usuario")
    list_select_related = ("empresa", "usuario")
    readonly_fields = (
        "id_evento",
        "empresa",
        "usuario",
        "modulo",
        "accion",
        "tabla",
        "id_registro",
        "antes_json",
        "despues_json",
        "ip",
        "user_agent",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
