"""Cancelar una factura de venta: factura y CxC canceladas, contrapóliza del día."""

from django.db import transaction

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import CuentaPorCobrar, Factura, NotaCredito
from finanzas.services.factura_inventario_service import FacturaInventarioService
from finanzas.services.factura_poliza_service import FacturaPolizaService
from finanzas.services.factura_timbrado_service import (
    PENDIENTE_CFDI,
    FacturaTimbradoService,
    agregar_observacion,
    normalizar_motivo_sat,
)


class FacturaCancelacionService:
    @staticmethod
    def cancelar(factura, usuario=None, motivo="", motivo_sat=None, al_cancelar=None, al_cancelar_cfdi=None):
        """Regresa la contrapóliza, o ``None`` si no hubo (Borrador, ya cancelada o sin póliza).

        Los cobros aplicados no se tocan: su póliza sigue y en Clientes queda el
        importe cobrado como saldo a favor. Las piezas facturadas regresan a existencia
        (solo lo que el pedido descontó y no se ha reintegrado) y se vuelven a
        descontar al refacturarlas (#513).

        Timbrada: primero el SAT, y ``cfdi_cancelado`` se guarda en su propia
        transacción. Si la parte del ERP falla, reintentar no vuelve a llamar al
        SAT y el ERP no pierde que el CFDI ya está cancelado (#554).

        ``motivo_sat`` (02/03) es obligatorio solo si hay que cancelar el CFDI ante el
        SAT; ``motivo`` es la nota interna que va a ``observaciones`` (EC-806).

        No se llama dentro de un ``atomic``: la cancelación ante el SAT necesita sus propias
        transacciones cortas, y una exterior desharía la reserva y el resultado (EC-972).

        ``al_cancelar(bloqueada, estatus_antes, contrapoliza)``: corre en la transacción de la
        cancelación en el ERP, con la fila bloqueada, solo si la factura pasó a Cancelada (no
        en un reintento ante el SAT de una ya cancelada). Si falla, la factura no se cancela.

        ``al_cancelar_cfdi(factura)``: en una factura ya Cancelada en el ERP con CFDI vigente
        (#567), corre después de que el SAT respondió ``canceled`` (con cualquier otro resultado
        ``cancelar_cfdi`` levanta antes de llegar ahí).
        """
        motivo_sat = normalizar_motivo_sat(motivo_sat)
        actual = Factura.objects.get(pk=factura.pk)
        if not actual.activo:  # #857, como emitir/ y timbrar/
            raise ErrorDeNegocio({"estatus": "La factura está eliminada y no se puede cancelar."})
        if actual.estatus == Factura.FacturaStatus.CANCELADA:
            # #567: cancelada en el ERP antes de cancelar su CFDI; se cancela ahora.
            if actual.cfdi_uuid and not actual.cfdi_cancelado:
                # La nota interna ya no se descarta (EC-806): va en la misma transacción que
                # ``cfdi_cancelado``, igual que al cancelar en el ERP.
                FacturaTimbradoService.cancelar_cfdi(
                    factura, motivo_sat, nota=f"CFDI cancelado ante el SAT: {motivo}" if motivo else ""
                )
                if al_cancelar_cfdi is not None:
                    al_cancelar_cfdi(factura)
            return None
        FacturaCancelacionService._exigir_sin_notas_emitidas(actual)
        if actual.cfdi_pendiente:  # #562, #904
            raise ErrorDeNegocio({"cfdi": PENDIENTE_CFDI})
        if actual.cfdi_uuid and not actual.cfdi_cancelado:
            FacturaTimbradoService.cancelar_cfdi(factura, motivo_sat)
        # Si ``_cancelar_en_erp`` (o ``al_cancelar``) falla de aquí en adelante, la factura queda
        # Emitida con ``cfdi_cancelado=True``: es el estado de reintento esperado (EC-972), no
        # una inconsistencia. ``cancelada_o_en_cancelacion`` ya la trata como cancelada, así que
        # no admite nota de crédito ni REP (``NotaCreditoService.validar_factura``,
        # ``construir_payload_rep``) ni cobros (``motivo_no_cobrable``); el reintento de
        # ``cancelar/`` no vuelve a llamar al SAT y termina la parte del ERP.
        return FacturaCancelacionService._cancelar_en_erp(factura, usuario, motivo, al_cancelar)

    @staticmethod
    def _exigir_sin_notas_emitidas(factura):
        if NotaCredito.objects.filter(
            factura=factura, estatus=NotaCredito.Estatus.EMITIDA
        ).exists():
            raise ErrorDeNegocio({"estatus": "La factura tiene notas de crédito emitidas: cancélalas primero."})

    @staticmethod
    @transaction.atomic
    def _cancelar_en_erp(factura, usuario, motivo, al_cancelar=None):
        bloqueada = Factura.objects.select_for_update().get(pk=factura.pk)
        if not bloqueada.activo:
            raise ErrorDeNegocio({"estatus": "La factura está eliminada y no se puede cancelar."})
        if bloqueada.estatus == Factura.FacturaStatus.CANCELADA:
            return None
        # Un ``timbrar/`` pudo reservar entre la lectura sin lock y ésta (#902).
        if bloqueada.cfdi_pendiente:
            raise ErrorDeNegocio({"cfdi": PENDIENTE_CFDI})
        FacturaCancelacionService._exigir_sin_notas_emitidas(bloqueada)

        antes = bloqueada.estatus
        era_emitida = antes == Factura.FacturaStatus.EMITIDA
        bloqueada.estatus = Factura.FacturaStatus.CANCELADA
        bloqueada.observaciones = agregar_observacion(bloqueada.observaciones, f"Cancelada: {motivo}" if motivo else "")
        bloqueada.save(update_fields=["estatus", "observaciones", "updated_at"])
        factura.estatus, factura.observaciones = bloqueada.estatus, bloqueada.observaciones

        CuentaPorCobrar.objects.filter(factura=bloqueada).update(
            estatus=CuentaPorCobrar.EstatusCxC.CANCELADA
        )
        # Un Borrador solo regresa lo que él mismo volvió a descontar al crearse.
        FacturaInventarioService.reintegrar(
            bloqueada, usuario, solo_redescontado=not era_emitida
        )
        contrapoliza = FacturaPolizaService.cancelar(bloqueada, usuario) if era_emitida else None
        if al_cancelar is not None:
            al_cancelar(bloqueada, antes, contrapoliza)
        return contrapoliza
