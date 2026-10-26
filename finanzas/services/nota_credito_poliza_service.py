"""Asiento de una nota de crédito: devolución e IVA contra Clientes."""

from django.utils import timezone

from decimal import Decimal

from finanzas.models import ConceptoContable, Poliza
from finanzas.services.poliza_motor_service import PolizaMotorService
from finanzas.utils.moneda import a_mxn

ORIGEN = "NotaCredito"


class NotaCreditoPolizaService:
    @staticmethod
    def generar(nota, usuario=None):
        """Póliza de la nota, o ``None`` si la empresa no contabiliza. En pesos, con el
        tipo de cambio de su factura (EC-581)."""
        factura = nota.factura
        folio = nota.folio or str(nota.pk)
        tc = factura.tipo_cambio_mxn() or Decimal("1")
        total, impuestos = a_mxn(nota.total, tc), a_mxn(nota.impuestos, tc)
        subtotal = nota.subtotal if tc == 1 else total - impuestos
        return PolizaMotorService.generar(
            empresa_id=factura.empresa_id,
            sucursal_id=factura.sucursal_id,
            origen_tipo=ORIGEN,
            origen_id=nota.pk,
            fecha=nota.fecha_emision or timezone.localdate(),
            tipo=Poliza.PolizaTipo.DIARIO,
            concepto=f"Nota de crédito {folio} - {nota.cliente.nombre}",
            lineas=[
                {"concepto": ConceptoContable.DEVOLUCIONES_VENTAS, "cargo": subtotal,
                 "referencia": folio},
                {"concepto": ConceptoContable.IVA_TRASLADADO, "cargo": impuestos,
                 "referencia": folio},
                {"concepto": ConceptoContable.CLIENTES, "abono": total,
                 "factura": factura, "referencia": folio},
            ],
            usuario=usuario,
        )

    @staticmethod
    def cancelar(nota, usuario=None):
        return PolizaMotorService.cancelar_de(ORIGEN, nota.pk, usuario)
