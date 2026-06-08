from django.contrib import admin
from .models import (
    Picking, PickingDetalle, Packing, 
    PackingDetalle, Despacho, DespachoDetalle, 
    ConteoCiclico, ConteoCiclicoDetalle, 
    Transferencia, TransferenciaDetalle
)

admin.site.register(Picking)
admin.site.register(PickingDetalle)
admin.site.register(Packing)
admin.site.register(PackingDetalle)
admin.site.register(Despacho)
admin.site.register(DespachoDetalle)
admin.site.register(ConteoCiclico)
admin.site.register(ConteoCiclicoDetalle)
admin.site.register(Transferencia)
admin.site.register(TransferenciaDetalle)
