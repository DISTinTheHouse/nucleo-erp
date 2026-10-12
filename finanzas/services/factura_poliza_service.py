"""Asiento de una factura de venta: cargo a Clientes, abono a Ventas e IVA trasladado."""

from decimal import Decimal

from finanzas.models import ConceptoContable, Poliza
from finanzas.services.poliza_motor_service import PolizaMotorService

ORIGEN = "Factura"


class FacturaPolizaService:
    @staticmethod
    def lineas(factura):
        folio = factura.folio or str(factura.pk)
        referencia = folio
        neto = (factura.subtotal or Decimal("0")) - (factura.descuento or Decimal("0"))
        return [
            {
                "concepto": ConceptoContable.CLIENTES,
                "cargo": factura.total,
                "factura": factura,
                "referencia": referencia,
                "observaciones": f"Cargo por cuenta por cobrar de factura {folio}.",
            },
            {
                "concepto": ConceptoContable.INGRESO_VENTAS,
                "abono": neto,
                "factura": factura,
                "referencia": referencia,
                "observaciones": f"Abono por ingreso de factura {folio}.",
            },
            {
                "concepto": ConceptoContable.IVA_TRASLADADO,
                "abono": factura.impuestos,
                "factura": factura,
                "referencia": referencia,
                "observaciones": f"Abono por impuestos de factura {folio}.",
            },
        ]

    @staticmethod
    def previsualizar(factura):
        return PolizaMotorService.previsualizar(factura.empresa_id, FacturaPolizaService.lineas(factura))

    @staticmethod
    def generar(factura, usuario=None):
        """Póliza de la factura, o ``None`` si la empresa no contabiliza."""
        return PolizaMotorService.generar(
            empresa_id=factura.empresa_id,
            sucursal_id=factura.sucursal_id,
            origen_tipo=ORIGEN,
            origen_id=factura.pk,
            fecha=factura.fecha_emision,
            tipo=Poliza.PolizaTipo.INGRESO,
            concepto=f"Factura por cobrar {factura.folio or factura.pk} - {factura.cliente.nombre}",
            lineas=FacturaPolizaService.lineas(factura),
            usuario=usuario,
        )
