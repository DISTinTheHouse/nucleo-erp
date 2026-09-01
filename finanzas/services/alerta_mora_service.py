from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from finanzas.models import (
    AlertaMora,
    CuentaPorCobrar,
    CuentaPorPagar,
)


def _nivel_mora(dias: int) -> str:
    if dias <= 7:
        return AlertaMora.Nivel.LEVE
    if dias <= 30:
        return AlertaMora.Nivel.MODERADA
    if dias <= 60:
        return AlertaMora.Nivel.GRAVE
    return AlertaMora.Nivel.CRITICA


class AlertaMoraService:
    @staticmethod
    @transaction.atomic
    def generar_alertas(empresa=None, fecha_hoy=None):
        hoy = fecha_hoy or timezone.localdate()
        AlertaMora.objects.filter(empresa=empresa).delete() if empresa else None

        cxc_qs = CuentaPorCobrar.objects.exclude(
            estatus__in=[
                CuentaPorCobrar.EstatusCxC.PAGADA,
                CuentaPorCobrar.EstatusCxC.CANCELADA,
            ]
        ).filter(saldo__gt=0, fecha_vencimiento__isnull=False, fecha_vencimiento__lt=hoy)
        if empresa:
            cxc_qs = cxc_qs.filter(empresa=empresa)

        for cxc in cxc_qs:
            dias = (hoy - cxc.fecha_vencimiento).days
            if dias <= 0:
                continue
            emp = cxc.empresa_id
            if emp is None:
                emp = getattr(cxc.factura, "empresa_id", None)
            if emp is None:
                continue
            AlertaMora.objects.create(
                empresa_id=emp,
                tipo_cuenta=AlertaMora.TipoCuenta.COBRAR,
                cuenta_por_cobrar=cxc,
                dias_mora=dias,
                nivel=_nivel_mora(dias),
                fecha_generada=timezone.now(),
            )

        cxp_qs = CuentaPorPagar.objects.exclude(
            estatus__in=[
                CuentaPorPagar.EstatusCxP.PAGADA,
                CuentaPorPagar.EstatusCxP.CANCELADA,
            ]
        ).filter(saldo__gt=0, fecha_vencimiento__isnull=False, fecha_vencimiento__lt=hoy)
        if empresa:
            cxp_qs = cxp_qs.filter(empresa=empresa)

        for cxp in cxp_qs:
            dias = (hoy - cxp.fecha_vencimiento).days
            if dias <= 0:
                continue
            AlertaMora.objects.create(
                empresa_id=cxp.empresa_id,
                tipo_cuenta=AlertaMora.TipoCuenta.PAGAR,
                cuenta_por_pagar=cxp,
                dias_mora=dias,
                nivel=_nivel_mora(dias),
                fecha_generada=timezone.now(),
            )

        total_creadas = AlertaMora.objects.filter(empresa=empresa).count() if empresa else AlertaMora.objects.count()
        return total_creadas
