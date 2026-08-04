from django.contrib import admin
from .models import (
    Picking, PickingDetalle, Packing,
    PackingDetalle, Despacho, DespachoDetalle,
    ConteoCiclico, ConteoCiclicoDetalle,
    Transferencia, TransferenciaDetalle,
    EtiquetaRFIDImpresion, EtiquetaRFIDDetalle,
)


admin.site.register(Picking)
admin.site.register(PickingDetalle)
admin.site.register(Packing)
admin.site.register(PackingDetalle)
admin.site.register(Despacho)
admin.site.register(DespachoDetalle)
admin.site.register(ConteoCiclico)
admin.site.register(ConteoCiclicoDetalle)
admin.site.register(Transferencia)
admin.site.register(TransferenciaDetalle)


class EtiquetaRFIDDetalleInline(admin.TabularInline):
    model = EtiquetaRFIDDetalle
    extra = 0
    fields = ("epc", "barcode_value", "serial", "estado", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    show_change_link = True
    classes = ("collapse",)


@admin.register(EtiquetaRFIDImpresion)
class EtiquetaRFIDImpresionAdmin(admin.ModelAdmin):
    list_display = (
        "folio",
        "empresa",
        "sucursal",
        "usuario",
        "producto",
        "producto_variante",
        "cantidad",
        "rfid_mode",
        "printer_name",
        "status",
        "created_at",
    )
    list_filter = (
        "status",
        "rfid_mode",
        "empresa",
        "sucursal",
        "created_at",
    )
    search_fields = (
        "id",
        "producto__nombre",
        "producto__codigo",
        "producto__cod_proscai",
        "producto_variante__sku",
        "producto_variante__nombre",
        "usuario__email",
        "usuario__username",
        "printer_name",
        "printer_address",
    )
    readonly_fields = ("folio", "created_at", "updated_at")
    raw_id_fields = (
        "empresa",
        "sucursal",
        "usuario",
        "producto",
        "producto_variante",
    )
    fieldsets = (
        (
            "Identificación",
            {"fields": ("folio", "empresa", "sucursal", "usuario", "created_at", "updated_at")},
        ),
        (
            "Producto",
            {"fields": ("producto", "producto_variante", "cantidad")},
        ),
        (
            "Impresión",
            {"fields": ("rfid_mode", "printer_name", "printer_address", "status")},
        ),
        (
            "Contenido",
            {"fields": ("zpl_enviado", "observaciones")},
        ),
    )
    inlines = (EtiquetaRFIDDetalleInline,)
    ordering = ("-created_at", "-id")
    show_full_result_count = False


@admin.register(EtiquetaRFIDDetalle)
class EtiquetaRFIDDetalleAdmin(admin.ModelAdmin):
    list_display = (
        "__str__",
        "impresion",
        "epc",
        "barcode_value",
        "serial",
        "estado",
        "created_at",
    )
    list_filter = ("estado", "created_at")
    search_fields = (
        "epc",
        "barcode_value",
        "serial",
        "impresion__id",
    )
    raw_id_fields = ("impresion",)
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at", "-id")
    show_full_result_count = False
