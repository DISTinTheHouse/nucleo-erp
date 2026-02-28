from django.contrib import admin
from inventarios.models import Almacen, Ubicacion, Lote, Serie, AjusteInventario, Existencia, MovimientoInventario

admin.site.register(Almacen)
admin.site.register(Ubicacion)
admin.site.register(Lote)
admin.site.register(Serie)
admin.site.register(AjusteInventario)
admin.site.register(Existencia)
admin.site.register(MovimientoInventario)