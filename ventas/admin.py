from django.contrib import admin
from ventas.models import (
    Prospecto,
    Oportunidad,
    Cotizacion,
    Pedido,
    PedidoDetalle,
    Entrega,
    EntregaDetalle,
    Devolucion,
    DevolucionDetalle,
)


@admin.register(Prospecto)
class ProspectoAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa")
    list_filter = ("empresa",)
    search_fields = ("id", "empresa__codigo", "empresa__razon_social")
    ordering = ("-id",)
    autocomplete_fields = ("empresa",)
    list_select_related = ("empresa",)


@admin.register(Oportunidad)
class OportunidadAdmin(admin.ModelAdmin):
    list_display = ("id", "prospecto")
    list_filter = ("prospecto__empresa",)
    search_fields = ("id", "prospecto__id", "prospecto__empresa__codigo", "prospecto__empresa__razon_social")
    ordering = ("-id",)
    autocomplete_fields = ("prospecto",)
    list_select_related = ("prospecto", "prospecto__empresa")


@admin.register(Cotizacion)
class CotizacionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "empresa",
        "sucursal",
        "cliente",
        "vendedor",
        "estatus",
        "monto",
        "moneda",
        "created_at",
    )
    list_filter = (
        "empresa",
        "sucursal",
        "moneda",
        "estatus",
        "forma_pago",
        "metodo_pago",
        "uso_cfdi",
        "created_at",
    )
    search_fields = (
        "id",
        "oc",
        "persona_pagos",
        "correo_facturas",
        "telefono_pagos",
        "destinatario",
        "empresa_envio",
        "direccion_envio",
        "ciudad_envio",
        "estado_envio",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
        "cliente__rfc",
        "cliente__razon_social",
        "cliente__nombre",
        "vendedor__username",
        "vendedor__email",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    autocomplete_fields = ("empresa", "vendedor", "sucursal", "cliente", "oportunidad", "moneda")
    list_select_related = ("empresa", "vendedor", "sucursal", "cliente", "oportunidad", "moneda")


class PedidoDetalleInline(admin.TabularInline):
    model = PedidoDetalle
    extra = 0
    autocomplete_fields = ("producto",)


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = (
        "folio",
        "empresa",
        "sucursal",
        "cliente",
        "estatus",
        "tipo_pedido",
        "moneda",
        "gran_total",
        "activo",
        "created_at",
        "fecha_confirmacion",
    )
    list_filter = (
        "empresa",
        "sucursal",
        "moneda",
        "tipo_pedido",
        "estatus",
        "activo",
        "created_at",
    )
    search_fields = (
        "id",
        "folio",
        "cliente__rfc",
        "cliente__razon_social",
        "cliente__nombre",
        "cliente_razon_social",
        "cliente_nombre",
        "cliente_rfc",
        "oc",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    autocomplete_fields = (
        "empresa",
        "sucursal",
        "serie_folio",
        "cliente",
        "cotizacion",
        "moneda",
        "cliente_regimen_fiscal",
    )
    list_select_related = (
        "empresa",
        "sucursal",
        "serie_folio",
        "cliente",
        "cotizacion",
        "moneda",
        "cliente_regimen_fiscal",
    )
    inlines = (PedidoDetalleInline,)


@admin.register(PedidoDetalle)
class PedidoDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "pedido", "producto", "precio_unitario", "subtotal_linea")
    list_filter = ("pedido__empresa", "pedido__sucursal", "producto")
    search_fields = ("id", "pedido__folio", "pedido__id", "producto__nombre", "producto__id")
    ordering = ("-id",)
    autocomplete_fields = ("pedido", "producto")
    list_select_related = ("pedido", "producto")


@admin.register(Entrega)
class EntregaAdmin(admin.ModelAdmin):
    list_display = ("id", "pedido")
    list_filter = ("pedido__empresa", "pedido__sucursal")
    search_fields = ("id", "pedido__id", "pedido__folio")
    ordering = ("-id",)
    autocomplete_fields = ("pedido",)
    list_select_related = ("pedido", "pedido__empresa", "pedido__sucursal")


@admin.register(EntregaDetalle)
class EntregaDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "entrega", "pedido_detalle")
    list_filter = ("entrega__pedido__empresa", "entrega__pedido__sucursal")
    search_fields = (
        "id",
        "entrega__id",
        "entrega__pedido__folio",
        "pedido_detalle__id",
        "pedido_detalle__producto__nombre",
    )
    ordering = ("-id",)
    autocomplete_fields = ("entrega", "pedido_detalle")
    list_select_related = ("entrega", "pedido_detalle")


@admin.register(Devolucion)
class DevolucionAdmin(admin.ModelAdmin):
    list_display = ("id", "pedido", "entrega")
    list_filter = ("pedido__empresa", "pedido__sucursal")
    search_fields = ("id", "pedido__id", "pedido__folio", "entrega__id")
    ordering = ("-id",)
    autocomplete_fields = ("entrega", "pedido")
    list_select_related = ("entrega", "pedido", "pedido__empresa", "pedido__sucursal")


@admin.register(DevolucionDetalle)
class DevolucionDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "devolucion", "entrega_detalle", "pedido_detalle")
    list_filter = ("devolucion__pedido__empresa", "devolucion__pedido__sucursal")
    search_fields = (
        "id",
        "devolucion__id",
        "devolucion__pedido__folio",
        "entrega_detalle__id",
        "pedido_detalle__id",
        "pedido_detalle__producto__nombre",
    )
    ordering = ("-id",)
    autocomplete_fields = ("devolucion", "entrega_detalle", "pedido_detalle")
    list_select_related = ("devolucion", "entrega_detalle", "pedido_detalle")
