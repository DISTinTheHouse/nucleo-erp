from django.contrib import admin
from django.db.models import Count, Sum
from django.utils.html import format_html
from .models import (
    Picking, PickingDetalle, Packing,
    PackingDetalle, Despacho, DespachoDetalle,
    ConteoCiclico, ConteoCiclicoDetalle,
    Transferencia, TransferenciaDetalle,
    EtiquetaRFIDImpresion, EtiquetaRFIDDetalle,
    RfidScan,
)


admin.site.register(Picking)
admin.site.register(PickingDetalle)
admin.site.register(Packing)
admin.site.register(PackingDetalle)
admin.site.register(Despacho)
admin.site.register(DespachoDetalle)
admin.site.register(ConteoCiclico)
admin.site.register(ConteoCiclicoDetalle)


class TransferenciaDetalleInline(admin.TabularInline):
    model = TransferenciaDetalle
    extra = 0
    fields = (
        "producto",
        "producto_variante",
        "cantidad",
        "ubicacion_origen",
        "ubicacion_destino",
        "lote",
        "serie",
    )
    raw_id_fields = (
        "producto",
        "producto_variante",
        "ubicacion_origen",
        "ubicacion_destino",
        "lote",
        "serie",
    )
    readonly_fields = fields
    can_delete = False
    show_change_link = True
    classes = ("collapse",)


@admin.register(Transferencia)
class TransferenciaAdmin(admin.ModelAdmin):
    inlines = (TransferenciaDetalleInline,)

    list_display = (
        "id",
        "folio",
        "status",
        "empresa",
        "sucursal",
        "almacen_origen",
        "almacen_destino",
        "usuario_nombre",
        "total_renglones",
        "total_cantidad",
        "fecha_creacion",
    )
    list_display_links = ("id", "folio")
    list_filter = (
        "status",
        "empresa",
        "sucursal",
        "almacen_origen",
        "almacen_destino",
        "fecha_creacion",
    )
    search_fields = (
        "id",
        "folio",
        "observaciones",
        "usuario__email",
        "usuario__username",
        "sucursal__nombre",
        "almacen_origen__codigo",
        "almacen_origen__nombre",
        "almacen_destino__codigo",
        "almacen_destino__nombre",
        "transferencia_detalle__producto__nombre",
        "transferencia_detalle__producto__codigo",
        "transferencia_detalle__producto_variante__sku",
        "transferencia_detalle__producto_variante__nombre",
    )
    readonly_fields = (
        "folio",
        "fecha_creacion",
        "usuario_nombre",
        "total_renglones",
        "total_cantidad",
    )
    raw_id_fields = (
        "empresa",
        "sucursal",
        "almacen_origen",
        "almacen_destino",
        "usuario",
    )
    fieldsets = (
        (
            "Identificación",
            {
                "fields": (
                    ("id", "folio"),
                    "status",
                    ("empresa", "sucursal"),
                    "fecha_creacion",
                    ("usuario", "usuario_nombre"),
                )
            },
        ),
        (
            "Almacenes",
            {
                "fields": (
                    "almacen_origen",
                    "almacen_destino",
                )
            },
        ),
        (
            "Totales",
            {
                "classes": ("collapse",),
                "fields": (
                    "total_renglones",
                    "total_cantidad",
                ),
            },
        ),
        (
            "Observaciones",
            {"fields": ("observaciones",)},
        ),
    )
    ordering = ("-fecha_creacion", "-id")
    show_full_result_count = False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "empresa",
                "sucursal",
                "almacen_origen",
                "almacen_destino",
                "usuario",
            )
            .annotate(
                _total_renglones=Count("transferencia_detalle"),
                _total_cantidad=Sum("transferencia_detalle__cantidad"),
            )
        )

    def usuario_nombre(self, obj):
        u = obj.usuario
        if not u:
            return "-"
        nombre = (u.get_full_name() or "").strip()
        return nombre or u.email
    usuario_nombre.short_description = "Usuario"
    usuario_nombre.admin_order_field = "usuario"

    def total_renglones(self, obj):
        return getattr(obj, "_total_renglones", 0) or 0
    total_renglones.short_description = "Renglones"

    def total_cantidad(self, obj):
        val = getattr(obj, "_total_cantidad", None)
        if val is None:
            return "-"
        return format_html("{:.4f}", val)
    total_cantidad.short_description = "Total Pzs"


@admin.register(TransferenciaDetalle)
class TransferenciaDetalleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "transferencia",
        "transferencia_folio",
        "transferencia_status",
        "producto",
        "producto_variante",
        "cantidad",
        "ubicacion_origen",
        "ubicacion_destino",
        "lote",
        "serie",
    )
    list_filter = (
        "transferencia__status",
        "transferencia__empresa",
        "transferencia__sucursal",
        "transferencia__almacen_origen",
        "transferencia__almacen_destino",
        "transferencia__fecha_creacion",
    )
    search_fields = (
        "id",
        "transferencia__id",
        "transferencia__folio",
        "producto__nombre",
        "producto__codigo",
        "producto_variante__sku",
        "producto_variante__nombre",
        "lote__codigo",
        "serie__codigo",
    )
    raw_id_fields = (
        "transferencia",
        "producto",
        "producto_variante",
        "ubicacion_origen",
        "ubicacion_destino",
        "lote",
        "serie",
    )
    readonly_fields = list_display
    ordering = ("-id",)
    show_full_result_count = False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "transferencia",
                "producto",
                "producto_variante",
                "ubicacion_origen",
                "ubicacion_destino",
                "lote",
                "serie",
            )
        )

    def transferencia_folio(self, obj):
        return getattr(obj.transferencia, "folio", "-")
    transferencia_folio.short_description = "Folio"

    def transferencia_status(self, obj):
        return getattr(obj.transferencia, "status", "-")
    transferencia_status.short_description = "Estatus"


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


@admin.register(RfidScan)
class RfidScanAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "epc",
        "reader_ip",
        "antenna",
        "rssi",
        "created_at",
    )
    list_filter = ("created_at", "antenna", "reader_ip")
    search_fields = ("epc", "reader_ip", "id")
    readonly_fields = ("created_at",)
    ordering = ("-created_at", "-id")
    show_full_result_count = False
