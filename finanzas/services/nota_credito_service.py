from decimal import Decimal

from django.db import transaction

from finanzas.exceptions import ErrorDeNegocio
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
            raise ErrorDeNegocio(
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

    @staticmethod
    @transaction.atomic
    def cancelar_nota_credito(nota: NotaCredito):
        """Inverso de ``aplicar_nota_credito``: devuelve a la CxC lo acreditado.

        Cancelar sólo revertía el estatus de la nota, así que el saldo de la CxC
        quedaba rebajado para siempre. Mismo patrón que
        ``CobroService.cancelar_cobro``/``PagoService.cancelar_pago``.
        """
        if nota.estatus == NotaCredito.Estatus.CANCELADA:
            return
        # Sólo se revierte lo que realmente se aplicó: una nota en Borrador
        # nunca tocó la CxC.
        if nota.estatus == NotaCredito.Estatus.EMITIDA:
            cxc = (
                CuentaPorCobrar.objects.select_for_update()
                .filter(factura=nota.factura)
                .first()
            )
            total_nc = Decimal(str(nota.total or 0))
            if cxc is not None and total_nc > 0:
                cxc.saldo = (
                    Decimal(str(cxc.saldo or 0)) + total_nc
                ).quantize(Decimal("0.01"))
                if cxc.saldo >= Decimal(str(cxc.total or 0)) - Decimal("0.01"):
                    cxc.estatus = CuentaPorCobrar.EstatusCxC.PENDIENTE
                else:
                    cxc.estatus = CuentaPorCobrar.EstatusCxC.PARCIAL
                cxc.save()
        nota.estatus = NotaCredito.Estatus.CANCELADA
        nota.save()
