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
        # Orden de bloqueo de todo el módulo: primero la nota, después la CxC.
        # El estatus se relee de la fila ya bloqueada porque el objeto que llega
        # pudo cargarse antes de que otra petición emitiera la misma nota.
        # Si la fila ya no está, ``get`` levanta ``DoesNotExist``: es preferible
        # fallar ruidosamente a omitir en silencio la aplicación de un crédito.
        bloqueada = NotaCredito.objects.select_for_update().get(pk=nota.pk)
        if bloqueada.estatus != NotaCredito.Estatus.EMITIDA:
            return
        cxc = (
            CuentaPorCobrar.objects.select_for_update()
            .filter(factura_id=bloqueada.factura_id)
            .first()
        )
        if cxc is None:
            # Sin CxC no hay nada que acreditar: emitir devolvía 201 y dejaba la
            # nota como Emitida sin haber aplicado nada.
            raise ErrorDeNegocio(
                {
                    "factura": (
                        "La factura de la nota no tiene una cuenta por cobrar; "
                        "no hay saldo al cual aplicar el crédito."
                    )
                }
            )
        total_nc = Decimal(str(bloqueada.total or 0))
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

        El guard de doble cancelación se evalúa con la fila de la nota
        bloqueada (mismo orden nota -> CxC que ``aplicar_nota_credito``): antes
        se leía del objeto en memoria, cargado fuera del lock, así que dos
        cancelaciones concurrentes pasaban ambas el guard y devolvían el
        importe a la CxC dos veces.
        """
        # La fila puede haber desaparecido entre la lectura del llamador y el
        # lock. ``DoesNotExist`` sube hasta la vista, que responde 404: salir en
        # silencio devolvía 200 "cancelada" sin cancelar ni devolver el saldo.
        NotaCredito.objects.select_for_update().get(pk=nota.pk)
        # Ya con la fila tomada, se sincroniza el objeto del llamador: puede
        # traer un estatus previo a la cancelación de la otra petición, y es el
        # que se guarda y se serializa en la respuesta.
        nota.refresh_from_db()
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
