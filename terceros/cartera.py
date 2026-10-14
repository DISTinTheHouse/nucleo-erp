"""Cartera de un cliente: saldo por cobrar por moneda y por antigüedad del vencimiento."""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

CAMPOS = (
    "saldo_total", "por_vencer", "vencido_1_30", "vencido_31_60",
    "vencido_61_90", "vencido_91_120", "vencido_mas_120",
)


def cartera_de_cliente(cliente):
    """Una consulta; una fila por moneda con los saldos por antigüedad."""
    from finanzas.models import CuentaPorCobrar

    hoy = timezone.localdate()

    def vencido(desde, hasta):
        # ``desde``/``hasta`` en días de atraso, ambos incluidos; ``hasta=None`` = sin tope.
        filtro = Q(fecha_vencimiento__lte=hoy - timedelta(days=desde))
        if hasta is not None:
            filtro &= Q(fecha_vencimiento__gte=hoy - timedelta(days=hasta))
        return Sum("saldo", filter=filtro)

    filas = (
        CuentaPorCobrar.objects.filter(cliente=cliente, empresa_id=cliente.empresa_id, saldo__gt=0)
        .exclude(estatus=CuentaPorCobrar.EstatusCxC.CANCELADA)
        .order_by()
        .values("factura__moneda__codigo_iso")
        .annotate(
            saldo_total=Sum("saldo"),
            por_vencer=Sum("saldo", filter=Q(fecha_vencimiento__isnull=True) | Q(fecha_vencimiento__gte=hoy)),
            vencido_1_30=vencido(1, 30),
            vencido_31_60=vencido(31, 60),
            vencido_61_90=vencido(61, 90),
            vencido_91_120=vencido(91, 120),
            vencido_mas_120=vencido(121, None),
        )
    )
    return [
        {"moneda": f["factura__moneda__codigo_iso"],
         **{c: str(Decimal(f[c] or 0).quantize(Decimal("0.01"))) for c in CAMPOS}}
        for f in filas
    ]
