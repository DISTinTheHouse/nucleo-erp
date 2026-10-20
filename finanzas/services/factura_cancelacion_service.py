"""Cancelar una factura de venta: factura y CxC canceladas, contrapóliza del día."""

from django.db import transaction

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import CuentaPorCobrar, Factura, NotaCredito
from finanzas.services.factura_inventario_service import FacturaInventarioService
from finanzas.services.factura_poliza_service import FacturaPolizaService
from finanzas.services.factura_timbrado_service import FacturaTimbradoService


class FacturaCancelacionService:
    @staticmethod
    def cancelar(factura, usuario=None, motivo=""):
        """Regresa la contrapóliza, o ``None`` si no hubo (Borrador, ya cancelada o sin póliza).

        Los cobros aplicados no se tocan: su póliza sigue y en Clientes queda el
        importe cobrado como saldo a favor. Las piezas facturadas regresan a existencia
        (solo lo que el pedido descontó y no se ha reintegrado) y se vuelven a
        descontar al refacturarlas (#513).

        Timbrada: primero el SAT, y ``cfdi_cancelado`` se guarda en su propia
        transacción. Si la parte del ERP falla, reintentar no vuelve a llamar al
        SAT y el ERP no pierde que el CFDI ya está cancelado (#554).
        """
        actual = Factura.objects.get(pk=factura.pk)
        if actual.estatus == Factura.FacturaStatus.CANCELADA:
            return None
        FacturaCancelacionService._exigir_sin_notas_emitidas(actual)
        if actual.cfdi_pendiente:  # #562
            raise ErrorDeNegocio({"cfdi": "La factura tiene un timbrado sin confirmar: revísalo en Facturama antes de cancelarla."})
        if actual.cfdi_uuid and not actual.cfdi_cancelado:
            FacturaTimbradoService.cancelar_cfdi(factura)
        return FacturaCancelacionService._cancelar_en_erp(factura, usuario, motivo)

    @staticmethod
    def _exigir_sin_notas_emitidas(factura):
        if NotaCredito.objects.filter(
            factura=factura, estatus=NotaCredito.Estatus.EMITIDA
        ).exists():
            raise ErrorDeNegocio({"estatus": "La factura tiene notas de crédito emitidas: cancélalas primero."})

    @staticmethod
    @transaction.atomic
    def _cancelar_en_erp(factura, usuario, motivo):
        bloqueada = Factura.objects.select_for_update().get(pk=factura.pk)
        if bloqueada.estatus == Factura.FacturaStatus.CANCELADA:
            return None
        FacturaCancelacionService._exigir_sin_notas_emitidas(bloqueada)

        era_emitida = bloqueada.estatus == Factura.FacturaStatus.EMITIDA
        bloqueada.estatus = Factura.FacturaStatus.CANCELADA
        if motivo:
            bloqueada.observaciones = f"{bloqueada.observaciones or ''}\nCancelada: {motivo}".strip()
        bloqueada.save(update_fields=["estatus", "observaciones", "updated_at"])
        factura.estatus, factura.observaciones = bloqueada.estatus, bloqueada.observaciones

        CuentaPorCobrar.objects.filter(factura=bloqueada).update(
            estatus=CuentaPorCobrar.EstatusCxC.CANCELADA
        )
        # Un Borrador solo regresa lo que él mismo volvió a descontar al crearse.
        FacturaInventarioService.reintegrar(
            bloqueada, usuario, solo_redescontado=not era_emitida
        )
        return FacturaPolizaService.cancelar(bloqueada, usuario) if era_emitida else None
