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
    list_display = ('codigo_iso', 'nombre', 'estatus')
    search_fields = ('codigo_iso', 'nombre')

@admin.register(Impuesto)
class ImpuestoAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'nombre', 'tasa', 'tipo', 'estatus')
    search_fields = ('codigo', 'nombre')

@admin.register(UnidadMedida)
class UnidadMedidaAdmin(admin.ModelAdmin):
    list_display = ('clave', 'nombre', 'estatus')
    search_fields = ('clave', 'nombre')

@admin.register(Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'razon_social', 'rfc', 'estatus')
    search_fields = ('codigo', 'razon_social', 'rfc')
    list_filter = ('estatus',)

@admin.register(Sucursal)
class SucursalAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'empresa', 'codigo', 'estatus')
    list_filter = ('empresa', 'estatus')
    search_fields = ('nombre', 'codigo')

@admin.register(Departamento)
class DepartamentoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'sucursal', 'codigo', 'estatus')
    list_filter = ('empresa', 'sucursal')
    search_fields = ('nombre', 'codigo')

@admin.register(SerieFolio)
class SerieFolioAdmin(admin.ModelAdmin):
    list_display = ('tipo_documento', 'serie', 'folio_actual', 'sucursal')
    list_filter = ('empresa', 'tipo_documento')
    search_fields = ('serie', 'tipo_documento')

# SAT Catalogs
@admin.register(SatUsoCfdi)
class SatUsoCfdiAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'descripcion', 'estatus')
    search_fields = ('codigo', 'descripcion')

@admin.register(SatMetodoPago)
class SatMetodoPagoAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'descripcion', 'estatus')
    search_fields = ('codigo', 'descripcion')

@admin.register(SatFormaPago)
class SatFormaPagoAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'descripcion', 'bancarizado', 'estatus')
    search_fields = ('codigo', 'descripcion')

@admin.register(SatClaveProdServ)
class SatClaveProdServAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'descripcion', 'estatus')
    search_fields = ('codigo', 'descripcion')

@admin.register(SatClaveUnidad)
class SatClaveUnidadAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'descripcion', 'estatus')
    search_fields = ('codigo', 'descripcion')

@admin.register(SatRegimenFiscal)
class SatRegimenFiscalAdmin(admin.ModelAdmin):
    list_display = ('codigo', 'descripcion', 'aplica_fisica', 'aplica_moral', 'estatus')
    search_fields = ('codigo', 'descripcion')

@admin.register(EmpresaSatConfig)
class EmpresaSatConfigAdmin(admin.ModelAdmin):
    list_display = ('empresa', 'regimen_fiscal', 'fecha_expiracion')
    search_fields = ('empresa__razon_social', 'regimen_fiscal__codigo')
