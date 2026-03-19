from django.contrib import admin
from ventas.models import Prospecto, Oportunidad, Cotizacion, Pedido, PedidoDetalle, Entrega, EntregaDetalle, Devolucion, DevolucionDetalle

admin.site.register(Prospecto)
admin.site.register(Oportunidad)
admin.site.register(Cotizacion)
admin.site.register(Pedido)
admin.site.register(PedidoDetalle)
admin.site.register(Entrega)
admin.site.register(EntregaDetalle)
admin.site.register(Devolucion)
admin.site.register(DevolucionDetalle)
