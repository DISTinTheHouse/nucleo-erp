"""Emitir una factura de venta: Borrador -> Emitida, con su CFDI, su CxC y su póliza."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import CuentaPorCobrar, Factura
from finanzas.services.factura_poliza_service import FacturaPolizaService
from finanzas.services.factura_timbrado_service import FacturaTimbradoService


class FacturaEmisionService:
    @staticmethod
    def emitir(factura, usuario=None):
        """Emite la factura y regresa ``(cxc, poliza)``; la póliza es ``None`` si la empresa no contabiliza.

        La emite una persona, no el alta: ``onboarding/`` y ``desde-pedido/`` dejan la
        factura en Borrador. Emitir fecha la factura hoy y fija su vencimiento con el
        plazo de crédito del cliente si no traía uno.

        Si a la empresa le toca timbrar (``FacturaTimbradoService.aplica``), el CFDI va
        primero y en su propia transacción: si Facturama lo rechaza la factura sigue
        en Borrador, y si lo timbra pero la parte contable falla, reintentar no la
        vuelve a timbrar.
        """
        if FacturaTimbradoService.aplica(factura):
            FacturaEmisionService._validar(Factura.objects.get(pk=factura.pk))
            FacturaTimbradoService.timbrar(factura)
        return FacturaEmisionService._emitir(factura, usuario)

    @staticmethod
    def _validar(factura):
        if not factura.activo:
            raise ErrorDeNegocio({"estatus": "La factura está eliminada y no se puede emitir."})
        if factura.estatus != Factura.FacturaStatus.BORRADOR:
            raise ErrorDeNegocio(
                {"estatus": f"Solo se puede emitir una factura en Borrador (esta está {factura.estatus})."}
            )
        if (factura.total or 0) <= 0:
            raise ErrorDeNegocio({"total": "La factura debe tener un total mayor a 0 para emitirse."})

    @staticmethod
    @transaction.atomic
    def _emitir(factura, usuario):
        bloqueada = Factura.objects.select_for_update().select_related("cliente").get(pk=factura.pk)
        FacturaEmisionService._validar(bloqueada)

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
