from decimal import Decimal

from django.db import transaction
from django.core.exceptions import ValidationError

from finanzas.models import (
    CuentaPorCobrar,
    NotaCredito,
)


class NotaCreditoService:
    @staticmethod
    @transaction.atomic
    def aplicar_nota_credito(nota: NotaCredito):
        if nota.estatus != NotaCredito.Estatus.EMITIDA:
            return
        cxc = (
            CuentaPorCobrar.objects.select_for_update()
            .filter(factura=nota.factura)
            .first()
        )
        if cxc is None:
            return
        total_nc = Decimal(str(nota.total or 0))
        if total_nc <= 0:
            return
        saldo = Decimal(str(cxc.saldo or 0))
        if total_nc > saldo + Decimal("0.0001"):
            raise ValidationError(
                {
                    "total": (
                        f"El total de la nota ({total_nc}) no puede superar "
                        f"el saldo de la CxC ({saldo})."
                    )
                }
            )
        cxc.saldo = (saldo - total_nc).quantize(Decimal("0.01"))
        if cxc.saldo <= Decimal("0.00"):
            cxc.saldo = Decimal("0.00")
            cxc.estatus = CuentaPorCobrar.EstatusCxC.PAGADA
        else:
            cxc.estatus = CuentaPorCobrar.EstatusCxC.PARCIAL
        cxc.save()
