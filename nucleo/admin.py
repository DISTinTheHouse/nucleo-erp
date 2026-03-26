from django.contrib import admin
from .models import (
    Moneda, Impuesto, UnidadMedida,
    Empresa, Sucursal, Departamento, SerieFolio,
    SatUsoCfdi, SatMetodoPago, SatFormaPago, SatRegimenFiscal,
    SatClaveProdServ, SatClaveUnidad,
    EmpresaSatConfig
)


@admin.register(Moneda)
class MonedaAdmin(admin.ModelAdmin):
    list_display = ("codigo_iso", "nombre", "empresa", "activo")
    list_filter = ("empresa", "activo")
    search_fields = ("codigo_iso", "nombre", "empresa__codigo", "empresa__razon_social")
    ordering = ("codigo_iso", "id")
    autocomplete_fields = ("empresa",)
    list_select_related = ("empresa",)

@admin.register(Impuesto)
class ImpuestoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre", "tasa", "tipo", "activo")
    list_filter = ("tipo", "activo")
    search_fields = ("codigo", "nombre")
    ordering = ("codigo", "id")

@admin.register(UnidadMedida)
class UnidadMedidaAdmin(admin.ModelAdmin):
    list_display = ("clave", "nombre", "activo")
    list_filter = ("activo",)
    search_fields = ("clave", "nombre")
    ordering = ("clave", "id")

@admin.register(Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    list_display = ("codigo", "razon_social", "nombre_comercial", "rfc", "activo", "created_at")
    list_filter = ("activo",)
    search_fields = ("codigo", "razon_social", "nombre_comercial", "rfc", "email_contacto", "telefono")
    date_hierarchy = "created_at"
    ordering = ("codigo", "id_empresa")

@admin.register(Sucursal)
class SucursalAdmin(admin.ModelAdmin):
    list_display = ("nombre", "empresa", "codigo", "activo", "created_at")
    list_filter = ("empresa", "activo")
    search_fields = ("nombre", "codigo", "empresa__codigo", "empresa__razon_social")
    date_hierarchy = "created_at"
    ordering = ("empresa", "nombre", "id_sucursal")
    autocomplete_fields = ("empresa",)
    list_select_related = ("empresa",)

@admin.register(Departamento)
class DepartamentoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "empresa", "sucursal", "codigo", "activo", "created_at")
    list_filter = ("empresa", "sucursal", "activo")
    search_fields = (
        "nombre",
        "codigo",
        "empresa__codigo",
        "empresa__razon_social",
        "sucursal__codigo",
        "sucursal__nombre",
    )
    date_hierarchy = "created_at"
    ordering = ("empresa", "sucursal", "nombre", "id_departamento")
    autocomplete_fields = ("empresa", "sucursal")
    list_select_related = ("empresa", "sucursal")

@admin.register(SerieFolio)
class SerieFolioAdmin(admin.ModelAdmin):
    list_display = ("tipo_documento", "serie", "folio_actual", "empresa", "sucursal", "activo")
    list_filter = ("empresa", "sucursal", "tipo_documento", "activo", "incluir_anio", "reiniciar_anual")
    search_fields = ("serie", "tipo_documento", "empresa__codigo", "empresa__razon_social", "sucursal__codigo", "sucursal__nombre")
    ordering = ("empresa", "tipo_documento", "serie", "id_serie_folio")
    autocomplete_fields = ("empresa", "sucursal")
    list_select_related = ("empresa", "sucursal")

# SAT Catalogs
@admin.register(SatUsoCfdi)
class SatUsoCfdiAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "activo")
    list_filter = ("activo",)
    search_fields = ("codigo", "descripcion")
    ordering = ("codigo", "id_sat_uso_cfdi")

@admin.register(SatMetodoPago)
class SatMetodoPagoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "activo")
    list_filter = ("activo",)
    search_fields = ("codigo", "descripcion")
    ordering = ("codigo", "id_sat_metodo_pago")

@admin.register(SatFormaPago)
class SatFormaPagoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "bancarizado", "activo")
    list_filter = ("bancarizado", "activo")
    search_fields = ("codigo", "descripcion")
    ordering = ("codigo", "id_sat_forma_pago")

@admin.register(SatClaveProdServ)
class SatClaveProdServAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "activo")
    list_filter = ("activo",)
    search_fields = ("codigo", "descripcion")
    ordering = ("codigo", "id_sat_prodserv")

@admin.register(SatClaveUnidad)
class SatClaveUnidadAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "activo")
    list_filter = ("activo",)
    search_fields = ("codigo", "descripcion")
    ordering = ("codigo", "id_sat_unidad")

@admin.register(SatRegimenFiscal)
class SatRegimenFiscalAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "aplica_fisica", "aplica_moral", "activo")
    list_filter = ("aplica_fisica", "aplica_moral", "activo")
    search_fields = ("codigo", "descripcion")
    ordering = ("codigo", "id_sat_regimen_fiscal")

@admin.register(EmpresaSatConfig)
class EmpresaSatConfigAdmin(admin.ModelAdmin):
    list_display = ("empresa", "regimen_fiscal", "validado", "activo", "fecha_expiracion")
    list_filter = ("activo", "validado", "regimen_fiscal")
    search_fields = (
        "empresa__codigo",
        "empresa__razon_social",
        "regimen_fiscal__codigo",
        "regimen_fiscal__descripcion",
        "no_certificado",
    )
    date_hierarchy = "fecha_expiracion"
    ordering = ("empresa", "id_empresa_sat_config")
    autocomplete_fields = ("empresa", "regimen_fiscal")
    list_select_related = ("empresa", "regimen_fiscal")
