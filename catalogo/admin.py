from django.contrib import admin
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante

@admin.register(TipoProducto)
class TipoProductoAdmin(admin.ModelAdmin):
    list_display = ("id", "codigo")
    search_fields = ("codigo",)
    ordering = ("codigo", "id")


@admin.register(CategoriaProducto)
class CategoriaProductoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo", "empresa", "activo", "created_at")
    list_filter = ("empresa", "activo", "created_at")
    search_fields = ("nombre", "codigo", "descripcion", "empresa__codigo", "empresa__razon_social")
    ordering = ("nombre", "id")
    autocomplete_fields = ("empresa",)
    list_select_related = ("empresa",)


@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo_hex", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre", "codigo_hex")
    ordering = ("nombre", "id")


@admin.register(Talla)
class TallaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre",)
    ordering = ("nombre", "id")


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = (
        "nombre",
        "empresa",
        "categoria_producto",
        "tipo",
        "precio_base",
        "activo",
        "created_at",
    )
    list_filter = (
        "empresa",
        "activo",
        "categoria_producto",
        "unidad_medida",
        "impuesto",
        "sat_prodserv",
        "sat_unidad",
        "created_at",
    )
    search_fields = (
        "nombre",
        "descripcion",
        "tipo",
        "empresa__codigo",
        "empresa__razon_social",
        "categoria_producto__nombre",
        "unidad_medida__nombre",
        "impuesto__nombre",
        "sat_prodserv__codigo",
        "sat_prodserv__descripcion",
        "sat_unidad__codigo",
        "sat_unidad__descripcion",
    )
    ordering = ("nombre", "id")
    autocomplete_fields = (
        "empresa",
        "categoria_producto",
        "unidad_medida",
        "impuesto",
        "sat_prodserv",
        "sat_unidad",
    )
    list_select_related = (
        "empresa",
        "categoria_producto",
        "unidad_medida",
        "impuesto",
        "sat_prodserv",
        "sat_unidad",
    )


@admin.register(ProductoVariante)
class ProductoVarianteAdmin(admin.ModelAdmin):
    list_display = ("sku", "producto", "color", "talla", "empresa", "precio_base", "activo")
    list_filter = ("empresa", "activo", "color", "talla")
    search_fields = ("sku", "producto__nombre", "color__nombre", "talla__nombre", "empresa__codigo")
    ordering = ("sku", "id")
    autocomplete_fields = ("producto", "empresa", "color", "talla")
    list_select_related = ("producto", "empresa", "color", "talla")
