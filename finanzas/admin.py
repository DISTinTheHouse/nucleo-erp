from django.contrib import admin

from finanzas.models import (
    AlertaMora,
    Banco,
    CentroCosto,
    Cobro,
    CobroDetalle,
    ConciliacionBancaria,
    ConciliacionDetalle,
    CuentaBancaria,
    CuentaContable,
    CuentaPorCobrar,
    CuentaPorPagar,
    Factura,
    FacturaDetalle,
    FacturaProveedor,
    FacturaProveedorDetalle,
    MovimientoBancario,
    NotaCredito,
    NotaCreditoDetalle,
    Pago,
    PagoDetalle,
    Poliza,
    PolizaDetalle,
)


@admin.register(CuentaContable)
class CuentaContableAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre", "empresa", "tipo", "nivel", "cuenta_padre", "acepta_movimientos", "activo")
    list_filter = ("empresa", "tipo", "nivel", "acepta_movimientos", "activo")
    search_fields = ("codigo", "nombre", "empresa__codigo", "empresa__razon_social")
    ordering = ("empresa", "codigo")
    autocomplete_fields = ("empresa", "cuenta_padre")
    list_select_related = ("empresa", "cuenta_padre")


@admin.register(CentroCosto)
class CentroCostoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre", "empresa", "activo")
    list_filter = ("empresa", "activo")
    search_fields = ("codigo", "nombre", "descripcion", "empresa__codigo", "empresa__razon_social")
    ordering = ("empresa", "codigo")
    autocomplete_fields = ("empresa",)
    list_select_related = ("empresa",)


class PolizaDetalleInline(admin.TabularInline):
    model = PolizaDetalle
    fk_name = "poliza"
    extra = 0
    fields = ("cuenta_contable", "centro_costo", "cargo", "abono", "referencia", "orden")
    autocomplete_fields = ("cuenta_contable", "centro_costo")


@admin.register(Poliza)
class PolizaAdmin(admin.ModelAdmin):
    list_display = ("folio", "empresa", "sucursal", "centro_costo", "tipo", "estatus", "fecha", "usuario_creacion", "activo")
    list_filter = ("empresa", "sucursal", "tipo", "estatus", "activo")
    search_fields = ("folio", "concepto", "empresa__codigo", "empresa__razon_social", "sucursal__codigo", "sucursal__nombre")
    date_hierarchy = "fecha"
    ordering = ("-fecha", "-folio_consecutivo")
    autocomplete_fields = ("empresa", "sucursal", "centro_costo", "usuario_creacion")
    list_select_related = ("empresa", "sucursal", "centro_costo", "usuario_creacion")
    inlines = (PolizaDetalleInline,)


@admin.register(PolizaDetalle)
class PolizaDetalleAdmin(admin.ModelAdmin):
    """Vista plana de las líneas, además del inline en Póliza -- útil para
    buscar un cargo/abono suelto sin conocer de antemano su póliza."""
    list_display = ("id", "poliza", "cuenta_contable", "centro_costo", "cargo", "abono", "orden")
    list_filter = ("poliza__empresa", "cuenta_contable")
    search_fields = ("poliza__folio", "cuenta_contable__codigo", "cuenta_contable__nombre", "referencia")
    ordering = ("poliza", "orden", "id")
    autocomplete_fields = ("poliza", "cuenta_contable", "centro_costo", "factura", "factura_proveedor", "pago", "cobro", "movimiento_bancario")
    list_select_related = ("poliza", "cuenta_contable", "centro_costo")


class FacturaDetalleInline(admin.TabularInline):
    model = FacturaDetalle
    extra = 0
    autocomplete_fields = ("pedido_detalle", "producto")


@admin.register(FacturaDetalle)
class FacturaDetalleAdmin(admin.ModelAdmin):
    """Registrado aparte (no solo como inline): NotaCreditoDetalle referencia
    una línea de factura puntual y necesita poder buscarla por autocomplete."""
    list_display = ("id", "factura", "producto", "cantidad", "precio_unitario", "total")
    list_filter = ("factura__empresa", "producto")
    search_fields = ("factura__folio", "producto__nombre", "producto__codigo")
    ordering = ("-id",)
    autocomplete_fields = ("factura", "pedido_detalle", "producto")
    list_select_related = ("factura", "pedido_detalle", "producto")


@admin.register(Factura)
class FacturaAdmin(admin.ModelAdmin):
    list_display = ("folio", "empresa", "sucursal", "cliente", "pedido", "estatus", "moneda", "total", "activo", "fecha_emision")
    list_filter = ("empresa", "sucursal", "moneda", "estatus", "activo")
    search_fields = (
        "folio",
        "cliente__rfc",
        "cliente__razon_social",
        "cliente__nombre",
        "pedido__folio",
        "empresa__codigo",
        "empresa__razon_social",
    )
    date_hierarchy = "fecha_emision"
    ordering = ("-fecha_emision", "-id")
    autocomplete_fields = ("empresa", "sucursal", "cliente", "pedido", "serie_folio", "moneda")
    list_select_related = ("empresa", "sucursal", "cliente", "pedido", "serie_folio", "moneda")
    inlines = (FacturaDetalleInline,)


class FacturaProveedorDetalleInline(admin.TabularInline):
    model = FacturaProveedorDetalle
    extra = 0
    autocomplete_fields = ("oc_detalle", "recepcion_detalle", "producto")


@admin.register(FacturaProveedor)
class FacturaProveedorAdmin(admin.ModelAdmin):
    list_display = ("folio", "empresa", "sucursal", "proveedor", "oc", "recepcion", "estatus", "moneda", "total", "activo", "fecha_emision")
    list_filter = ("empresa", "sucursal", "moneda", "estatus", "activo")
    search_fields = (
        "folio",
        "proveedor__rfc",
        "proveedor__razon_social",
        "proveedor__nombre",
        "oc__folio",
        "empresa__codigo",
        "empresa__razon_social",
    )
    date_hierarchy = "fecha_emision"
    ordering = ("-fecha_emision", "-id")
    autocomplete_fields = ("empresa", "sucursal", "proveedor", "oc", "recepcion", "moneda")
    list_select_related = ("empresa", "sucursal", "proveedor", "oc", "recepcion", "moneda")
    inlines = (FacturaProveedorDetalleInline,)


@admin.register(Banco)
class BancoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo", "swift", "empresa", "activo")
    list_filter = ("empresa", "activo")
    search_fields = ("nombre", "codigo", "swift", "empresa__codigo", "empresa__razon_social")
    ordering = ("nombre",)
    autocomplete_fields = ("empresa",)
    list_select_related = ("empresa",)


@admin.register(CuentaBancaria)
class CuentaBancariaAdmin(admin.ModelAdmin):
    list_display = ("alias", "banco", "empresa", "moneda", "numero_cuenta", "saldo_actual", "activo")
    list_filter = ("empresa", "banco", "moneda", "activo")
    search_fields = ("alias", "titular", "numero_cuenta", "clabe", "numero_cliente", "banco__nombre", "empresa__codigo", "empresa__razon_social")
    ordering = ("empresa", "alias")
    autocomplete_fields = ("empresa", "banco", "moneda")
    list_select_related = ("empresa", "banco", "moneda")
    # Open Banking (token/refresh_token) no está en uso todavía (ver comentario
    # en el modelo) -- fuera del admin para no exponer credenciales de API por
    # esta vía el día que sí se pueblen.
    exclude = ("token", "refresh_token")


@admin.register(CuentaPorCobrar)
class CuentaPorCobrarAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "cliente", "factura", "total", "saldo", "estatus", "fecha_emision", "fecha_vencimiento")
    list_filter = ("empresa", "estatus")
    search_fields = ("cliente__rfc", "cliente__razon_social", "cliente__nombre", "factura__folio", "referencia")
    date_hierarchy = "fecha_emision"
    ordering = ("-fecha_emision", "-id")
    autocomplete_fields = ("empresa", "cliente", "factura")
    list_select_related = ("empresa", "cliente", "factura")


@admin.register(CuentaPorPagar)
class CuentaPorPagarAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "proveedor", "factura_proveedor", "total", "saldo", "estatus", "fecha_emision", "fecha_vencimiento")
    list_filter = ("empresa", "estatus")
    search_fields = ("proveedor__rfc", "proveedor__razon_social", "proveedor__nombre", "factura_proveedor__folio")
    date_hierarchy = "fecha_emision"
    ordering = ("-fecha_emision", "-id")
    autocomplete_fields = ("empresa", "proveedor", "factura_proveedor")
    list_select_related = ("empresa", "proveedor", "factura_proveedor")


class CobroDetalleInline(admin.TabularInline):
    model = CobroDetalle
    extra = 0
    autocomplete_fields = ("cxc",)


@admin.register(Cobro)
class CobroAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "cliente", "cuenta_bancaria", "fecha_cobro", "metodo_pago", "total_cobrado", "estatus", "activo")
    list_filter = ("empresa", "metodo_pago", "estatus", "activo")
    search_fields = ("cliente__rfc", "cliente__razon_social", "cliente__nombre", "referencia_operacion")
    date_hierarchy = "fecha_cobro"
    ordering = ("-fecha_cobro", "-id")
    autocomplete_fields = ("empresa", "cliente", "cuenta_bancaria")
    list_select_related = ("empresa", "cliente", "cuenta_bancaria")
    inlines = (CobroDetalleInline,)


class PagoDetalleInline(admin.TabularInline):
    model = PagoDetalle
    extra = 0
    autocomplete_fields = ("cxp",)


@admin.register(Pago)
class PagoAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "proveedor", "cuenta_bancaria", "fecha_pago", "metodo_pago", "total_pagado", "estatus", "activo")
    list_filter = ("empresa", "metodo_pago", "estatus", "activo")
    search_fields = ("proveedor__rfc", "proveedor__razon_social", "proveedor__nombre", "referencia_operacion")
    date_hierarchy = "fecha_pago"
    ordering = ("-fecha_pago", "-id")
    autocomplete_fields = ("empresa", "proveedor", "cuenta_bancaria")
    list_select_related = ("empresa", "proveedor", "cuenta_bancaria")
    inlines = (PagoDetalleInline,)


@admin.register(MovimientoBancario)
class MovimientoBancarioAdmin(admin.ModelAdmin):
    list_display = ("id", "cuenta_bancaria", "fecha", "tipo_movimiento", "importe", "saldo", "origen", "estatus")
    list_filter = ("cuenta_bancaria__empresa", "tipo_movimiento", "origen", "estatus")
    search_fields = ("concepto", "referencia", "cuenta_bancaria__alias")
    date_hierarchy = "fecha"
    ordering = ("-fecha", "-id")
    autocomplete_fields = ("cuenta_bancaria", "pago", "cobro")
    list_select_related = ("cuenta_bancaria", "pago", "cobro")


class ConciliacionDetalleInline(admin.TabularInline):
    model = ConciliacionDetalle
    extra = 0
    autocomplete_fields = ("movimiento_bancario",)


@admin.register(ConciliacionBancaria)
class ConciliacionBancariaAdmin(admin.ModelAdmin):
    list_display = ("id", "cuenta_bancaria", "fecha_inicio", "fecha_final", "saldo_estado_cuenta", "saldo_libros", "estatus")
    list_filter = ("cuenta_bancaria__empresa", "estatus")
    search_fields = ("cuenta_bancaria__alias",)
    date_hierarchy = "fecha_final"
    ordering = ("-fecha_final", "-id")
    autocomplete_fields = ("cuenta_bancaria",)
    list_select_related = ("cuenta_bancaria",)
    inlines = (ConciliacionDetalleInline,)


class NotaCreditoDetalleInline(admin.TabularInline):
    model = NotaCreditoDetalle
    extra = 0
    autocomplete_fields = ("factura_detalle",)


@admin.register(NotaCredito)
class NotaCreditoAdmin(admin.ModelAdmin):
    list_display = ("id", "folio", "factura", "cliente", "motivo", "total", "estatus", "fecha_emision")
    list_filter = ("estatus",)
    search_fields = ("folio", "motivo", "cliente__rfc", "cliente__razon_social", "cliente__nombre", "factura__folio")
    date_hierarchy = "fecha_emision"
    ordering = ("-fecha_emision", "-id")
    autocomplete_fields = ("factura", "cliente")
    list_select_related = ("factura", "cliente")
    inlines = (NotaCreditoDetalleInline,)


@admin.register(AlertaMora)
class AlertaMoraAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "tipo_cuenta", "nivel", "dias_mora", "notificado", "fecha_generada")
    list_filter = ("empresa", "tipo_cuenta", "nivel", "notificado")
    search_fields = ("cuenta_por_cobrar__cliente__nombre", "cuenta_por_pagar__proveedor__nombre")
    date_hierarchy = "fecha_generada"
    ordering = ("-fecha_generada",)
    autocomplete_fields = ("empresa", "cuenta_por_cobrar", "cuenta_por_pagar")
    list_select_related = ("empresa", "cuenta_por_cobrar", "cuenta_por_pagar")
