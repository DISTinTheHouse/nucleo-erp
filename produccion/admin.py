from django.contrib import admin
from produccion.models import (
    ListaMaterialBom,
    BomDetalle,
    RutaProduccion, 
    OrdenProduccion,
    OrdenProduccionDetalle,
    ConsumoProduccion,
    ConsumoProduccionDetalle,
    ProductoTerminadoEntradas, 
    OrdenesBordado,
    OrdenBordadoDetalle,
    BordadoAvances,
    BordadoIncidencias,
    OrdenesReflejante,
    ReflejanteAvances,
    ReflejanteIncidencias,
    OrdenesCorteManga,
    OrdenCorteMangaDetalle
)

admin.site.register(BordadoAvances)
admin.site.register(BordadoIncidencias)
admin.site.register(OrdenesReflejante)
admin.site.register(ReflejanteAvances)
admin.site.register(ReflejanteIncidencias)
admin.site.register(OrdenProduccionDetalle)
admin.site.register(OrdenesCorteManga)
admin.site.register(OrdenCorteMangaDetalle)

@admin.register(ListaMaterialBom)
class ListaMaterialBomAdmin(admin.ModelAdmin):
    list_display = ("bom_id", "empresa", "producto_variante", "variante_produccion", "version", "activo")
    list_filter = ("empresa", "activo")
    search_fields = ("bom_id", "producto_variante__producto__nombre", "producto_variante__sku", "empresa__codigo", "empresa__razon_social")
    ordering = ("-bom_id",)
    autocomplete_fields = ("empresa", "producto_variante")
    list_select_related = ("empresa", "producto_variante")

@admin.register(BomDetalle)
class BomDetalleAdmin(admin.ModelAdmin):
    list_display = ("bom_detalle_id", "bom", "componente", "cantidad")
    list_filter = ("bom__empresa", "bom__producto_variante")
    search_fields = ("bom_detalle_id", "bom__bom_id", "bom__producto_variante__sku", "componente__nombre")
    ordering = ("-bom_detalle_id",)
    autocomplete_fields = ("bom", "componente")
    list_select_related = ("bom", "componente")

@admin.register(RutaProduccion)
class RutaProduccionAdmin(admin.ModelAdmin):
    list_display = ("ruta_produccion_id", "empresa", "producto")
    list_filter = ("empresa", "producto")
    search_fields = (
        "ruta_produccion_id",
        "producto__nombre",
        "producto__id",
        "empresa__codigo",
        "empresa__razon_social",
    )
    ordering = ("-ruta_produccion_id",)
    autocomplete_fields = ("empresa", "producto")
    list_select_related = ("empresa", "producto")

class ProductoTerminadoEntradasInline(admin.TabularInline):
    model = ProductoTerminadoEntradas
    extra = 0
    autocomplete_fields = ("almacen", "ubicacion")

class ConsumoProduccionInline(admin.TabularInline):
    model = ConsumoProduccion
    extra = 0


class ConsumoProduccionDetalleInline(admin.TabularInline):
    model = ConsumoProduccionDetalle
    extra = 0
    autocomplete_fields = ("producto",)

@admin.register(OrdenProduccion)
class OrdenProduccionAdmin(admin.ModelAdmin):
    list_display = ("op_id", "empresa", "sucursal", "pedido", "ruta_produccion")
    list_filter = ("empresa", "sucursal", "ruta_produccion")
    search_fields = (
        "op_id",
        "pedido__id",
        "pedido__folio",
        "ruta_produccion__ruta_produccion_id",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
    )
    ordering = ("-op_id",)
    autocomplete_fields = ("empresa", "sucursal", "pedido", "ruta_produccion")
    list_select_related = ("empresa", "sucursal", "pedido", "ruta_produccion")
    inlines = (ConsumoProduccionInline, ProductoTerminadoEntradasInline)

@admin.register(ConsumoProduccion)
class ConsumoProduccionAdmin(admin.ModelAdmin):
    list_display = ("consumo_produccion_id", "op")
    list_filter = ("op__empresa", "op__sucursal")
    search_fields = ("consumo_produccion_id", "op__op_id", "op__pedido__folio")
    ordering = ("-consumo_produccion_id",)
    autocomplete_fields = ("op",)
    list_select_related = ("op",)
    inlines = (ConsumoProduccionDetalleInline,)


@admin.register(ConsumoProduccionDetalle)
class ConsumoProduccionDetalleAdmin(admin.ModelAdmin):
    list_display = ("consumo_detalle_id", "consumo_produccion", "producto", "cantidad")
    list_filter = ("consumo_produccion__op__empresa", "consumo_produccion__op__sucursal")
    search_fields = (
        "consumo_detalle_id",
        "consumo_produccion__consumo_produccion_id",
        "consumo_produccion__op__op_id",
        "producto__nombre",
    )
    ordering = ("-consumo_detalle_id",)
    autocomplete_fields = ("consumo_produccion", "producto")
    list_select_related = ("consumo_produccion", "producto")

@admin.register(ProductoTerminadoEntradas)
class ProductoTerminadoEntradasAdmin(admin.ModelAdmin):
    list_display = ("pt_entrada_id", "op", "almacen", "ubicacion")
    list_filter = ("op__empresa", "op__sucursal", "almacen")
    search_fields = (
        "pt_entrada_id",
        "op__op_id",
        "op__pedido__folio",
        "almacen__codigo",
        "almacen__nombre",
        "ubicacion__pasillo",
        "ubicacion__rack",
        "ubicacion__nivel",
        "ubicacion__posicion",
    )
    ordering = ("-pt_entrada_id",)
    autocomplete_fields = ("op", "almacen", "ubicacion")
    list_select_related = ("op", "almacen", "ubicacion")

class OrdenBordadoDetalleInline(admin.TabularInline):
    model = OrdenBordadoDetalle
    extra = 0
    autocomplete_fields = ("producto", "talla", "color", "pedido_detalle")
    list_select_related = ("producto", "talla", "color")
    fields = (
        "pedido_detalle",
        "producto",
        "talla",
        "color",
        "cantidad",
        "posicion_bordado",
        "colores_hilo",
        "puntadas",
    )

@admin.register(OrdenesBordado)
class OrdenesBordadoAdmin(admin.ModelAdmin):
    list_display = ("id", "folio_bordado", "empresa", "sucursal", "pedido", "estatus_bordado", "usuario_asignado", "prioridad")
    list_filter = ("empresa", "sucursal", "estatus_bordado", "prioridad")
    search_fields = (
        "folio_bordado",
        "pedido__folio",
        "pedido__id",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
        "usuario_asignado__email",
        "usuario_asignado__first_name",
        "usuario_asignado__last_name",
    )
    ordering = ("-id",)
    autocomplete_fields = ("empresa", "sucursal", "pedido", "usuario_asignado")
    list_select_related = ("empresa", "sucursal", "pedido", "usuario_asignado")
    inlines = (OrdenBordadoDetalleInline,)

@admin.register(OrdenBordadoDetalle)
class OrdenBordadoDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "ob", "producto", "talla", "color", "cantidad", "posicion_bordado", "colores_hilo", "puntadas")
    list_filter = (
        "ob__empresa",
        "ob__sucursal",
        "ob__estatus_bordado",
        "talla",
        "color",
        "posicion_bordado",
    )
    search_fields = (
        "id",
        "ob__folio_bordado",
        "ob__pedido__folio",
        "producto__nombre",
        "producto__id",
        "talla__nombre",
        "color__nombre",
        "posicion_bordado",
    )
    ordering = ("-id",)
    autocomplete_fields = ("ob", "pedido_detalle", "producto", "talla", "color")
    list_select_related = ("ob", "producto", "talla", "color")
