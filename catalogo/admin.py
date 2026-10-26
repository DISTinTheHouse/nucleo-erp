from django import forms
from django.contrib import admin
from catalogo.compatibilidad import normalizar_ubicacion
from catalogo.models import (
    TipoProducto, CategoriaProducto, CategoriaProductoProceso, CategoriaProductoTalla, Color, Talla, Producto,
    ProductoVariante,
)


# --- Matriz prenda/proceso (EC-572) -------------------------------------------
# La empresa sale de la categoría y ``ubicacion`` se guarda en mayúsculas, como en
# la API (``CategoriaProductoProcesoSerializer``).
class CategoriaProductoProcesoForm(forms.ModelForm):
    class Meta:
        model = CategoriaProductoProceso
        fields = ("categoria_producto", "proceso", "ubicacion", "activo")
        help_texts = {"ubicacion": "Vacía = cualquier ubicación. Ej.: HOMBROS, RODILLAS, A."}

    def clean_ubicacion(self):
        return normalizar_ubicacion(self.cleaned_data.get("ubicacion"))

    def clean(self):
        # El mensaje de la API, no el nombre de la restricción de BD.
        datos = super().clean()
        categoria_id = getattr(datos.get("categoria_producto"), "pk", None) or self.instance.categoria_producto_id
        if datos.get("activo") and CategoriaProductoProceso.objects.filter(
            categoria_producto_id=categoria_id, proceso=datos.get("proceso"),
            ubicacion=datos.get("ubicacion", ""), activo=True,
        ).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Ya existe una regla activa para esa categoría, proceso y ubicación.")
        return datos


class CategoriaProductoProcesoInline(admin.TabularInline):
    model = CategoriaProductoProceso
    form = CategoriaProductoProcesoForm
    fields = ("proceso", "ubicacion", "activo")
    extra = 1
    verbose_name = "proceso permitido"
    verbose_name_plural = "Procesos permitidos (sin filas = sin restricción)"


@admin.register(CategoriaProductoProceso)
class CategoriaProductoProcesoAdmin(admin.ModelAdmin):
    form = CategoriaProductoProcesoForm
    list_display = ("categoria_producto", "proceso", "ubicacion", "activo", "empresa")
    list_filter = ("proceso", "activo", "empresa")
    search_fields = ("categoria_producto__nombre", "ubicacion")
    ordering = ("categoria_producto__nombre", "proceso", "ubicacion")
    autocomplete_fields = ("categoria_producto",)
    list_select_related = ("categoria_producto", "empresa")

    def save_model(self, request, obj, form, change):
        obj.empresa_id = obj.categoria_producto.empresa_id
        super().save_model(request, obj, form, change)

@admin.register(TipoProducto)
class TipoProductoAdmin(admin.ModelAdmin):
    list_display = ("id", "codigo")
    search_fields = ("codigo",)
    ordering = ("codigo", "id")


class CategoriaProductoTallaInline(admin.TabularInline):
    model = CategoriaProductoTalla
    extra = 1
    autocomplete_fields = ("talla",)


@admin.register(CategoriaProducto)
class CategoriaProductoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo", "empresa", "unidad_medida", "activo", "created_at")
    list_filter = ("empresa", "activo", "unidad_medida", "created_at")
    search_fields = ("nombre", "codigo", "descripcion", "empresa__codigo", "empresa__razon_social")
    ordering = ("nombre", "id")
    autocomplete_fields = ("empresa", "unidad_medida")
    list_select_related = ("empresa", "unidad_medida")
    inlines = (CategoriaProductoTallaInline, CategoriaProductoProcesoInline)

    def save_formset(self, request, form, formset, change):
        if formset.model is CategoriaProductoProceso:
            for obj in formset.save(commit=False):
                obj.empresa_id = form.instance.empresa_id
                obj.save()
            for obj in formset.deleted_objects:
                obj.delete()
            return
        super().save_formset(request, form, formset, change)


@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo_hex", "pantone", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre", "codigo_hex", "pantone")
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
        "sku",
        "nombre",
        "descripcion",
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
