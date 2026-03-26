from django.contrib import admin
from produccion.models import ListaMaterialBom, RutaProduccion, OrdenProduccion, ConsumoProduccion, ProductoTerminadoEntradas


@admin.register(ListaMaterialBom)
class ListaMaterialBomAdmin(admin.ModelAdmin):
    list_display = ("bom_id", "empresa", "producto")
    list_filter = ("empresa", "producto")
    search_fields = ("bom_id", "producto__nombre", "producto__id", "empresa__codigo", "empresa__razon_social")
    ordering = ("-bom_id",)
    autocomplete_fields = ("empresa", "producto")
    list_select_related = ("empresa", "producto")


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
