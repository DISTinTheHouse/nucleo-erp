"""Importes por renglón y por talla de pedidos y cotizaciones (#1121).

``subtotal_talla = cantidad × (precio de la talla o, si es nulo, del renglón)`` y
``subtotal_linea = Σ subtotal_talla``. Se calculan al guardar con dos UPDATE por
documento: la lectura del detalle no hace consultas extra.
"""

from django.db.models import DecimalField, F, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from ventas.models import CotizacionDetalle, CotizacionDetalleTalla, PedidoDetalle, PedidoDetalleTalla

_DEC = DecimalField(max_digits=12, decimal_places=2)


def _recalcular(Detalle, Talla, fk_detalle, filtro_detalles):
    precio_renglon = Detalle.objects.filter(pk=OuterRef(fk_detalle)).values("precio_unitario")[:1]
    Talla.objects.filter(**{f"{fk_detalle}__in": filtro_detalles.values("pk")}).update(
        subtotal_talla=F("cantidad") * Coalesce("precio_unitario", Subquery(precio_renglon), output_field=_DEC)
    )
    suma = (
        Talla.objects.filter(**{fk_detalle: OuterRef("pk")})
        .values(fk_detalle).annotate(s=Sum("subtotal_talla")).values("s")[:1]
    )
    filtro_detalles.update(subtotal_linea=Coalesce(Subquery(suma, output_field=_DEC), 0, output_field=_DEC))


def recalcular_pedido(pedido):
    _recalcular(PedidoDetalle, PedidoDetalleTalla, "pedido_detalle",
                PedidoDetalle.objects.filter(pedido=pedido))


def recalcular_cotizacion(cotizacion):
    _recalcular(CotizacionDetalle, CotizacionDetalleTalla, "cotizacion_detalle",
                CotizacionDetalle.objects.filter(cotizacion=cotizacion))
