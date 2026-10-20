"""Cancelar una factura de venta: factura y CxC canceladas, contrapóliza del día."""

from django.db import transaction

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import CuentaPorCobrar, Factura, NotaCredito
from finanzas.services.factura_inventario_service import FacturaInventarioService
from finanzas.services.factura_poliza_service import FacturaPolizaService
from finanzas.services.factura_timbrado_service import FacturaTimbradoService


class FacturaCancelacionService:
    @staticmethod
    @transaction.atomic
    def cancelar(factura, usuario=None, motivo=""):
        """Regresa la contrapóliza, o ``None`` si no hubo (Borrador, ya cancelada o sin póliza).

        Los cobros aplicados no se tocan: su póliza sigue y en Clientes queda el
        importe cobrado como saldo a favor. Las piezas facturadas regresan a existencia
        (solo lo que el pedido descontó y no se ha reintegrado) y se vuelven a
        descontar al refacturarlas (#513).
        """
        bloqueada = Factura.objects.select_for_update().get(pk=factura.pk)
        if bloqueada.estatus == Factura.FacturaStatus.CANCELADA:
            return None
        if NotaCredito.objects.filter(
            factura=bloqueada, estatus=NotaCredito.Estatus.EMITIDA
        ).exists():
            raise ErrorDeNegocio({"estatus": "La factura tiene notas de crédito emitidas: cancélalas primero."})
        # Timbrada: primero el SAT; si no queda cancelado, no se toca nada en el ERP.
        if bloqueada.cfdi_uuid:
            FacturaTimbradoService.cancelar_cfdi(bloqueada)

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
