from django.contrib import admin
from compras.models import (
    Requisicion,
    RequisicionDetalle,
    CotizacionProveedor,
    CotizacionProveedorDetalle,
    SolicitudCompra,
    SolicitudCompraDetalle,
    OrdenCompra,
    OrdenCompraDetalle,
    Recepcion,
    RecepcionDetalle,
)


class RequisicionDetalleInline(admin.TabularInline):
    model = RequisicionDetalle
    extra = 0
    autocomplete_fields = ("producto",)


@admin.register(Requisicion)
class RequisicionAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "sucursal", "departamento")
    list_filter = ("empresa", "sucursal", "departamento")
    search_fields = (
        "id",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
        "departamento__codigo",
        "departamento__nombre",
    )
    ordering = ("-id",)
    autocomplete_fields = ("empresa", "sucursal", "departamento")
    list_select_related = ("empresa", "sucursal", "departamento")
    inlines = (RequisicionDetalleInline,)


@admin.register(RequisicionDetalle)
class RequisicionDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "requisicion", "producto")
    list_filter = (
        "requisicion__empresa",
        "requisicion__sucursal",
        "requisicion__departamento",
        "producto",
    )
    search_fields = ("id", "requisicion__id", "producto__nombre", "producto__id")
    ordering = ("-id",)
    autocomplete_fields = ("requisicion", "producto")
    list_select_related = ("requisicion", "producto")


class CotizacionProveedorDetalleInline(admin.TabularInline):
    model = CotizacionProveedorDetalle
    extra = 0
    autocomplete_fields = ("requisicion_detalle", "producto")


@admin.register(CotizacionProveedor)
class CotizacionProveedorAdmin(admin.ModelAdmin):
    list_display = ("id", "proveedor", "requisicion", "moneda")
    list_filter = ("proveedor", "moneda")
    search_fields = (
        "id",
        "proveedor__codigo",
        "proveedor__nombre",
        "proveedor__rfc",
        "requisicion__id",
    )
    ordering = ("-id",)
    autocomplete_fields = ("proveedor", "requisicion", "moneda")
    list_select_related = ("proveedor", "requisicion", "moneda")
    inlines = (CotizacionProveedorDetalleInline,)


@admin.register(CotizacionProveedorDetalle)
class CotizacionProveedorDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "cotizacion_proveedor", "requisicion_detalle", "producto")
    list_filter = ("cotizacion_proveedor", "producto")
    search_fields = (
        "id",
        "cotizacion_proveedor__id",
        "requisicion_detalle__id",
        "producto__nombre",
        "producto__id",
    )
    ordering = ("-id",)
    autocomplete_fields = ("cotizacion_proveedor", "requisicion_detalle", "producto")
    list_select_related = ("cotizacion_proveedor", "requisicion_detalle", "producto")


class SolicitudCompraDetalleInline(admin.TabularInline):
    model = SolicitudCompraDetalle
    extra = 0
    autocomplete_fields = ("producto", "requisicion_detalle")


@admin.register(SolicitudCompra)
class SolicitudCompraAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "sucursal", "departamento", "requisicion")
    list_filter = ("empresa", "sucursal", "departamento")
    search_fields = (
        "id",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
        "departamento__codigo",
        "departamento__nombre",
        "requisicion__id",
    )
    ordering = ("-id",)
    autocomplete_fields = ("empresa", "sucursal", "departamento", "requisicion")
    list_select_related = ("empresa", "sucursal", "departamento", "requisicion")
    inlines = (SolicitudCompraDetalleInline,)


@admin.register(SolicitudCompraDetalle)
class SolicitudCompraDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "solicitud_compra", "producto", "requisicion_detalle")
    list_filter = (
        "solicitud_compra__empresa",
        "solicitud_compra__sucursal",
        "producto",
    )
    search_fields = (
        "id",
        "solicitud_compra__id",
        "producto__nombre",
        "producto__id",
        "requisicion_detalle__id",
    )
    ordering = ("-id",)
    autocomplete_fields = ("solicitud_compra", "producto", "requisicion_detalle")
    list_select_related = ("solicitud_compra", "producto", "requisicion_detalle")


class OrdenCompraDetalleInline(admin.TabularInline):
    model = OrdenCompraDetalle
    extra = 0
    autocomplete_fields = ("producto", "solicitud_compra_detalle", "requisicion_detalle")


@admin.register(OrdenCompra)
class OrdenCompraAdmin(admin.ModelAdmin):
    list_display = (
        "folio",
        "empresa",
        "sucursal",
        "proveedor",
        "estatus",
        "fecha_oc",
        "total",
        "activo",
        "created_at",
    )
    list_filter = ("empresa", "sucursal", "proveedor", "estatus", "activo", "fecha_oc")
    search_fields = (
        "folio",
        "referencia",
        "proveedor__codigo",
        "proveedor__nombre",
        "proveedor__rfc",
        "pedido__id",
        "pedido__folio",
        "usuario__username",
    )
    date_hierarchy = "fecha_oc"
    ordering = ("-fecha_oc", "-id")
    autocomplete_fields = (
        "empresa",
        "sucursal",
        "proveedor",
        "solicitud_compra",
        "moneda",
        "usuario",
        "pedido",
    )
    list_select_related = (
        "empresa",
        "sucursal",
        "proveedor",
        "solicitud_compra",
        "moneda",
        "usuario",
        "pedido",
    )
    inlines = (OrdenCompraDetalleInline,)


@admin.register(OrdenCompraDetalle)
class OrdenCompraDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "orden_compra", "producto", "solicitud_compra_detalle", "requisicion_detalle")
    list_filter = ("orden_compra__empresa", "orden_compra__sucursal", "producto")
    search_fields = (
        "id",
        "orden_compra__folio",
        "producto__nombre",
        "producto__id",
        "solicitud_compra_detalle__id",
        "requisicion_detalle__id",
    )
    ordering = ("-id",)
    autocomplete_fields = ("orden_compra", "producto", "solicitud_compra_detalle", "requisicion_detalle")
    list_select_related = ("orden_compra", "producto", "solicitud_compra_detalle", "requisicion_detalle")


class RecepcionDetalleInline(admin.TabularInline):
    model = RecepcionDetalle
    extra = 0
    autocomplete_fields = ("orden_compra_detalle", "producto", "ubicacion", "lote", "serie")


@admin.register(Recepcion)
class RecepcionAdmin(admin.ModelAdmin):
    list_display = (
        "folio",
        "tipo_origen",
        "orden_compra",
        "op",
        "empresa",
        "sucursal",
        "proveedor",
        "almacen",
        "estatus",
        "fecha_recepcion",
        "activo",
    )
    list_filter = ("tipo_origen", "empresa", "sucursal", "proveedor", "almacen", "estatus", "activo")
    search_fields = (
        "folio",
        "remision",
        "factura_referencia",
        "orden_compra__folio",
        "op__folio_op",
        "proveedor__codigo",
        "proveedor__nombre",
        "proveedor__rfc",
    )
    date_hierarchy = "fecha_recepcion"
    ordering = ("-fecha_recepcion", "-id")
    autocomplete_fields = (
        "orden_compra",
        "op",
        "empresa",
        "sucursal",
        "proveedor",
        "almacen",
        "transportista",
        "usuario",
    )
    list_select_related = (
        "orden_compra",
        "op",
        "empresa",
        "sucursal",
        "proveedor",
        "almacen",
        "transportista",
        "usuario",
    )
    inlines = (RecepcionDetalleInline,)


@admin.register(RecepcionDetalle)
class RecepcionDetalleAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "recepcion",
        "orden_compra_detalle",
        "orden_produccion_detalle",
        "producto",
        "producto_variante",
        "ubicacion",
        "lote",
        "serie",
    )
    list_filter = ("recepcion__empresa", "recepcion__sucursal", "producto", "ubicacion__almacen")
    search_fields = (
        "id",
        "recepcion__folio",
        "orden_compra_detalle__orden_compra__folio",
        "orden_produccion_detalle__op__folio_op",
        "producto__nombre",
        "producto__id",
        "ubicacion__pasillo",
        "ubicacion__rack",
        "ubicacion__nivel",
        "ubicacion__posicion",
        "lote__id",
        "serie__id",
    )
    ordering = ("-id",)
    autocomplete_fields = ("recepcion", "orden_compra_detalle", "producto", "ubicacion", "lote", "serie")
    list_select_related = (
        "recepcion",
        "orden_compra_detalle",
        "orden_produccion_detalle",
        "producto",
        "producto_variante",
        "ubicacion",
        "lote",
        "serie",
    )

