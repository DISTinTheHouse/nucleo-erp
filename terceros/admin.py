from django.contrib import admin
from terceros.models import Cliente, Proveedor, Transportista


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ("id", "razon_social", "nombre", "rfc", "empresa", "telefono", "correo", "ciudad", "estado", "activo")
    list_filter = ("empresa", "activo", "sat_regimen_fiscal", "sat_uso_cfdi", "vendedores")
    search_fields = (
        "id",
        "razon_social",
        "nombre",
        "rfc",
        "telefono",
        "correo",
        "direccion_fiscal",
        "colonia",
        "codigo_postal",
        "ciudad",
        "estado",
        "giro_empresarial",
        "empresa__codigo",
        "empresa__razon_social",
    )
    ordering = ("razon_social", "id")
    autocomplete_fields = ("empresa", "sat_regimen_fiscal", "sat_uso_cfdi")
    list_select_related = ("empresa", "sat_regimen_fiscal", "sat_uso_cfdi")
    filter_horizontal = ("vendedores",)


@admin.register(Proveedor)
class ProveedorAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre", "razon_social", "rfc", "empresa", "telefono", "email", "moneda", "activo", "fecha_alta")
    list_filter = ("empresa", "activo", "moneda", "sat_regimen_fiscal", "sat_forma_pago", "sat_metodo_pago")
    search_fields = (
        "id",
        "codigo",
        "nombre",
        "razon_social",
        "rfc",
        "email",
        "telefono",
        "contacto_principal",
        "empresa__codigo",
        "empresa__razon_social",
    )
    date_hierarchy = "fecha_alta"
    ordering = ("nombre", "id")
    autocomplete_fields = ("empresa", "sat_regimen_fiscal", "sat_forma_pago", "sat_metodo_pago", "moneda")
    list_select_related = ("empresa", "sat_regimen_fiscal", "sat_forma_pago", "sat_metodo_pago", "moneda")


@admin.register(Transportista)
class TransportistaAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa")
    list_filter = ("empresa",)
    search_fields = ("id", "empresa__codigo", "empresa__razon_social")
    ordering = ("-id",)
    autocomplete_fields = ("empresa",)
    list_select_related = ("empresa",)
