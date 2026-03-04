from django.contrib import admin
from produccion.models import ListaMaterialBom, RutaProduccion, OrdenProduccion, ConsumoProduccion, ProductoTerminadoEntradas

admin.site.register(ListaMaterialBom)
admin.site.register(RutaProduccion)
admin.site.register(OrdenProduccion)
admin.site.register(ConsumoProduccion)
admin.site.register(ProductoTerminadoEntradas)