"""Emitir una factura de venta: Borrador -> Emitida, con su CxC y su póliza."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import CuentaPorCobrar, Factura
from finanzas.services.factura_poliza_service import FacturaPolizaService


class FacturaEmisionService:
    @staticmethod
    @transaction.atomic
    def emitir(factura, usuario=None):
        """Emite la factura y regresa ``(cxc, poliza)``; la póliza es ``None`` si la empresa no contabiliza.

        La emite una persona, no el alta: ``onboarding/`` y ``desde-pedido/`` dejan la
        factura en Borrador. Emitir fecha la factura hoy y fija su vencimiento con el
        plazo de crédito del cliente si no traía uno.
        """
        bloqueada = Factura.objects.select_for_update().select_related("cliente", "moneda").get(pk=factura.pk)
        if not bloqueada.activo:
            raise ErrorDeNegocio({"estatus": "La factura está eliminada y no se puede emitir."})
        if bloqueada.estatus != Factura.FacturaStatus.BORRADOR:
            raise ErrorDeNegocio(
                {"estatus": f"Solo se puede emitir una factura en Borrador (esta está {bloqueada.estatus})."}
            )
        if (bloqueada.total or 0) <= 0:
            raise ErrorDeNegocio({"total": "La factura debe tener un total mayor a 0 para emitirse."})
        if bloqueada.tipo_cambio_mxn() is None:  # EC-581: la póliza va en pesos
            raise ErrorDeNegocio(
                {"tipo_cambio": f"La factura está en {bloqueada.moneda.codigo_iso}: captura su tipo de cambio antes de emitirla."}
            )

        hoy = timezone.localdate()
        bloqueada.estatus = Factura.FacturaStatus.EMITIDA
        bloqueada.fecha_emision = hoy
        if bloqueada.fecha_vencimiento is None and bloqueada.cliente.dias_credito:
            bloqueada.fecha_vencimiento = hoy + timedelta(days=bloqueada.cliente.dias_credito)
        bloqueada.save(update_fields=["estatus", "fecha_emision", "fecha_vencimiento", "updated_at"])

        cxc = CuentaPorCobrar.objects.create(
            empresa=bloqueada.empresa,
            cliente=bloqueada.cliente,
            factura=bloqueada,
            fecha_emision=hoy,
            fecha_vencimiento=bloqueada.fecha_vencimiento,
            total=bloqueada.total,
            saldo=bloqueada.total,
            estatus=CuentaPorCobrar.EstatusCxC.PENDIENTE,
            referencia=bloqueada.folio,
        )
        poliza = FacturaPolizaService.generar(bloqueada, usuario)

        factura.estatus = bloqueada.estatus
        factura.fecha_emision = bloqueada.fecha_emision
        factura.fecha_vencimiento = bloqueada.fecha_vencimiento
        return cxc, poliza
