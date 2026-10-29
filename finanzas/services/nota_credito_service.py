from decimal import Decimal

from django.db import transaction

from finanzas.exceptions import ErrorDeNegocio
from finanzas.services.nota_credito_poliza_service import NotaCreditoPolizaService
from finanzas.models import (
    CuentaPorCobrar,
    Factura,
    NotaCredito,
)


class NotaCreditoService:
    @staticmethod
    def _validar_cxc(cxc, total_nc):
        if cxc is None:
            # Sin CxC no hay nada que acreditar: emitir devolvía 201 sin aplicar nada.
            raise ErrorDeNegocio({"factura": (
                "La factura de la nota no tiene una cuenta por cobrar; "
                "no hay saldo al cual aplicar el crédito."
            )})
        if cxc.estatus == CuentaPorCobrar.EstatusCxC.CANCELADA:
            raise ErrorDeNegocio({"factura": "La cuenta por cobrar de la factura está cancelada."})
        saldo = Decimal(str(cxc.saldo or 0))
        if total_nc > saldo + Decimal("0.0001"):
            raise ErrorDeNegocio({"total": (
                f"El total de la nota ({total_nc}) no puede superar el saldo de la CxC ({saldo})."
            )})

    @staticmethod
    def validar_factura(factura):
        """No se emite ni timbra una nota sobre una factura cancelada o con cancelación en curso (#614)."""
        if factura.cancelada_o_en_cancelacion:
            raise ErrorDeNegocio({"factura": "La factura está cancelada o su cancelación está en proceso."})

    @staticmethod
    def validar_aplicable(factura_id, total):
        """Misma validación que ``aplicar_nota_credito``, sin escribir."""
        NotaCreditoService.validar_factura(Factura.objects.get(pk=factura_id))
        cxc = CuentaPorCobrar.objects.filter(factura_id=factura_id).first()
        NotaCreditoService._validar_cxc(cxc, Decimal(str(total or 0)))

    @staticmethod
    @transaction.atomic
    def aplicar_nota_credito(nota: NotaCredito, usuario=None, poliza=True):
        # ``poliza=False``: quien timbra la genera después, solo si Facturama no rechaza
        # (un rechazo regresa la nota a Borrador sin dejar póliza y contrapóliza, #723).
        # Orden de bloqueo de todo el módulo: primero la nota, después la CxC.
        # El estatus se relee de la fila ya bloqueada porque el objeto que llega
        # pudo cargarse antes de que otra petición emitiera la misma nota.
        # Si la fila ya no está, ``get`` levanta ``DoesNotExist``: es preferible
        # fallar ruidosamente a omitir en silencio la aplicación de un crédito.
        bloqueada = NotaCredito.objects.select_for_update().get(pk=nota.pk)
        if bloqueada.estatus != NotaCredito.Estatus.EMITIDA:
            return
        cxc = CuentaPorCobrar.objects.select_for_update().filter(factura_id=bloqueada.factura_id).first()
        total_nc = Decimal(str(bloqueada.total or 0))
        NotaCreditoService.validar_factura(bloqueada.factura)
        NotaCreditoService._validar_cxc(cxc, total_nc)
        if total_nc <= 0:
            return
        saldo = Decimal(str(cxc.saldo or 0))
        cxc.saldo = (saldo - total_nc).quantize(Decimal("0.01"))
        if cxc.saldo <= Decimal("0.00"):
            cxc.saldo = Decimal("0.00")
            cxc.estatus = CuentaPorCobrar.EstatusCxC.PAGADA
        else:
            cxc.estatus = CuentaPorCobrar.EstatusCxC.PARCIAL
        cxc.save()
        if poliza:
            NotaCreditoPolizaService.generar(bloqueada, usuario)

    @staticmethod
    def _devolver_saldo(nota):
        """Regresa a la CxC lo que la nota Emitida le acreditó."""
        cxc = CuentaPorCobrar.objects.select_for_update().filter(factura=nota.factura).first()
        total_nc = Decimal(str(nota.total or 0))
        if cxc is None or total_nc <= 0:
            return
        cxc.saldo = (Decimal(str(cxc.saldo or 0)) + total_nc).quantize(Decimal("0.01"))
        if cxc.saldo >= Decimal(str(cxc.total or 0)) - Decimal("0.01"):
            cxc.estatus = CuentaPorCobrar.EstatusCxC.PENDIENTE
        else:
            cxc.estatus = CuentaPorCobrar.EstatusCxC.PARCIAL
        cxc.save()

    @staticmethod
    @transaction.atomic
    def regresar_a_borrador(nota: NotaCredito):
        """Deshace una emisión que Facturama rechazó: devuelve el saldo y la nota vuelve a
        Borrador para corregirla. Solo para una emisión aplicada con ``poliza=False``."""
        NotaCredito.objects.select_for_update().get(pk=nota.pk)
        nota.refresh_from_db()
        if nota.estatus != NotaCredito.Estatus.EMITIDA or nota.cfdi_uuid or nota.cfdi_pendiente:
            return
        NotaCreditoService._devolver_saldo(nota)
        nota.estatus = NotaCredito.Estatus.BORRADOR
        nota.save()

    @staticmethod
    @transaction.atomic
    def cancelar_nota_credito(nota: NotaCredito, usuario=None):
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
        # El ERP no cancela CFDI de egreso: cancelar aquí dejaría el CFDI vigente ante el SAT (#612).
        if nota.cfdi_uuid or nota.cfdi_pendiente:
            raise ErrorDeNegocio({"cfdi": (
                "La nota tiene CFDI de egreso timbrado o pendiente de confirmar; "
                "cancélalo en Facturama antes de cancelarla aquí."
            )})
        # Sólo se revierte lo que realmente se aplicó: una nota en Borrador
        # nunca tocó la CxC.
        if nota.estatus == NotaCredito.Estatus.EMITIDA:
            NotaCreditoService._devolver_saldo(nota)
        era_emitida = nota.estatus == NotaCredito.Estatus.EMITIDA
        nota.estatus = NotaCredito.Estatus.CANCELADA
        nota.save()
        if era_emitida:
            NotaCreditoPolizaService.cancelar(nota, usuario)
