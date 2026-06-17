from django.contrib import admin
from inventarios.models import (
    Almacen,
    Ubicacion,
    Lote,
    Serie,
    AjusteInventario,
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
)


@admin.register(Almacen)
class AlmacenAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre", "empresa", "sucursal", "tipo_almacen", "estatus", "created_at")
    list_filter = ("empresa", "sucursal", "estatus", "tipo_almacen", "requiere_ubicacion", "permite_entrada", "permite_salida")
    search_fields = (
        "codigo",
        "nombre",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
    )
    ordering = ("nombre", "id_almacen")
    autocomplete_fields = ("empresa", "sucursal")
    list_select_related = ("empresa", "sucursal")


@admin.register(Ubicacion)
class UbicacionAdmin(admin.ModelAdmin):
    list_display = ("almacen", "tipo_ubicacion", "estatus", "pasillo", "rack", "nivel", "posicion", "orden_recorrido")
    list_filter = ("almacen", "tipo_ubicacion", "estatus", "bloqueada_entrada", "bloqueada_salida", "permite_mezcla_productos")
    search_fields = (
        "pasillo",
        "rack",
        "nivel",
        "posicion",
        "almacen__codigo",
        "almacen__nombre",
        "almacen__empresa__codigo",
    )
    ordering = ("almacen", "pasillo", "rack", "nivel", "posicion", "id_ubicacion")
    autocomplete_fields = ("almacen",)
    list_select_related = ("almacen", "almacen__empresa", "almacen__sucursal")


@admin.register(Lote)
class LoteAdmin(admin.ModelAdmin):
    list_display = ("id", "producto")
    list_filter = ("producto",)
    search_fields = ("id", "producto__nombre", "producto__id")
    ordering = ("-id",)
    autocomplete_fields = ("producto",)
    list_select_related = ("producto",)


@admin.register(Serie)
class SerieAdmin(admin.ModelAdmin):
    list_display = ("id", "producto")
    list_filter = ("producto",)
    search_fields = ("id", "producto__nombre", "producto__id")
    ordering = ("-id",)
    autocomplete_fields = ("producto",)
    list_select_related = ("producto",)


@admin.register(AjusteInventario)
class AjusteInventarioAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "sucursal", "almacen")
    list_filter = ("empresa", "sucursal", "almacen")
    search_fields = (
        "id",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
        "almacen__codigo",
        "almacen__nombre",
    )
    ordering = ("-id",)
    autocomplete_fields = ("empresa", "sucursal", "almacen")
    list_select_related = ("empresa", "sucursal", "almacen")


@admin.register(Existencia)
class ExistenciaAdmin(admin.ModelAdmin):
    list_display = ("id", "display_producto", "display_producto_variante", "almacen", "ubicacion", "stock")
    list_filter = ("almacen__empresa", "almacen", "producto", "producto_variante")
    search_fields = (
        "id",
        "producto__nombre",
        "producto__id",
        "producto_variante__producto__nombre",
        "producto_variante__producto__id",
        "almacen__codigo",
        "almacen__nombre",
        "ubicacion__pasillo",
        "ubicacion__rack",
        "ubicacion__nivel",
        "ubicacion__posicion",
    )
    ordering = ("-id",)
    autocomplete_fields = ("producto", "producto_variante", "almacen", "ubicacion")
    list_select_related = ("producto", "producto_variante__producto", "almacen", "ubicacion")

    @admin.display(description="Producto")
    def display_producto(self, obj):
        producto = obj.producto or getattr(obj.producto_variante, "producto", None)
        return getattr(producto, "nombre", None)

    @admin.display(description="Variante")
    def display_producto_variante(self, obj):
        return getattr(obj, "producto_variante_id", None)


class MovimientoInventarioDetalleInline(admin.TabularInline):
    model = MovimientoInventarioDetalle
    extra = 0
    autocomplete_fields = ("producto", "ubicacion_origen", "ubicacion_destino", "lote", "serie")


@admin.register(MovimientoInventario)
class MovimientoInventarioAdmin(admin.ModelAdmin):
    list_display = ("id", "empresa", "sucursal", "pedido", "entrega", "devolucion", "ajuste_inventario")
    list_filter = ("empresa", "sucursal")
    search_fields = (
        "id",
        "pedido__id",
        "pedido__folio",
        "entrega__id",
        "devolucion__id",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
    )
    ordering = ("-id",)
    autocomplete_fields = ("empresa", "sucursal", "pedido", "entrega", "devolucion", "ajuste_inventario")
    list_select_related = ("empresa", "sucursal", "pedido", "entrega", "devolucion", "ajuste_inventario")
    inlines = (MovimientoInventarioDetalleInline,)


@admin.register(MovimientoInventarioDetalle)
class MovimientoInventarioDetalleAdmin(admin.ModelAdmin):
    list_display = ("id", "movimiento_inventario", "producto", "ubicacion_origen", "ubicacion_destino", "lote", "serie")
    list_filter = ("movimiento_inventario__empresa", "producto", "ubicacion_origen__almacen", "ubicacion_destino__almacen")
    search_fields = (
        "id",
        "movimiento_inventario__id",
        "producto__nombre",
        "producto__id",
        "ubicacion_origen__pasillo",
        "ubicacion_origen__rack",
        "ubicacion_origen__nivel",
        "ubicacion_origen__posicion",
        "ubicacion_destino__pasillo",
        "ubicacion_destino__rack",
        "ubicacion_destino__nivel",
        "ubicacion_destino__posicion",
        "lote__id",
        "serie__id",
    )
    ordering = ("-id",)
    autocomplete_fields = ("movimiento_inventario", "producto", "ubicacion_origen", "ubicacion_destino", "lote", "serie")
    list_select_related = ("movimiento_inventario", "producto", "ubicacion_origen", "ubicacion_destino", "lote", "serie")
