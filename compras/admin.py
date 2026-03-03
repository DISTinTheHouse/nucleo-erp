from django.contrib import admin

from django.contrib import admin
from compras.models import (
    Requisicion, 
    RequisicionDetalle, 
    CotizacionProveedor, 
    CotizacionProveedorDetalle, 
    SolicitudCompra, 
    SolicitudCompraDetalle, 
    OrdenCompra, 
    OrdenCompraDetalle, 
    Recepcion, 
    RecepcionDetalle
)

admin.site.register(Requisicion)
admin.site.register(RequisicionDetalle)
admin.site.register(CotizacionProveedor)
admin.site.register(CotizacionProveedorDetalle)
admin.site.register(SolicitudCompra)
admin.site.register(SolicitudCompraDetalle)
admin.site.register(OrdenCompra)
admin.site.register(OrdenCompraDetalle)
admin.site.register(Recepcion)
admin.site.register(RecepcionDetalle)

