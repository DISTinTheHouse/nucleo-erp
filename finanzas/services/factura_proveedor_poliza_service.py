"""Asiento de una factura de proveedor: compra e IVA acreditable contra Proveedores."""

from decimal import Decimal

from finanzas.models import ConceptoContable, Poliza
from finanzas.services.poliza_motor_service import PolizaMotorService

ORIGEN = "FacturaProveedor"


class FacturaProveedorPolizaService:
    @staticmethod
    def generar(factura, usuario=None):
        """Póliza de la factura, o ``None`` si la empresa no contabiliza."""
        folio = factura.folio or str(factura.pk)
        neto = (factura.subtotal or Decimal("0")) - (factura.descuento or Decimal("0"))
        return PolizaMotorService.generar(
            empresa_id=factura.empresa_id,
            sucursal_id=factura.sucursal_id,
            origen_tipo=ORIGEN,
            origen_id=factura.pk,
            fecha=factura.fecha_emision,
            tipo=Poliza.PolizaTipo.DIARIO,
            concepto=f"Factura de proveedor {folio} - {factura.proveedor.nombre}",
            lineas=[
                {"concepto": ConceptoContable.COMPRAS_INVENTARIO, "cargo": neto,
                 "factura_proveedor": factura, "referencia": folio},
                {"concepto": ConceptoContable.IVA_ACREDITABLE, "cargo": factura.impuestos,
                 "factura_proveedor": factura, "referencia": folio},
                {"concepto": ConceptoContable.PROVEEDORES, "abono": factura.total,
                 "factura_proveedor": factura, "referencia": folio},
            ],
            usuario=usuario,
        )

    @staticmethod
    def cancelar(factura, usuario=None):
        return PolizaMotorService.cancelar_de(ORIGEN, factura.pk, usuario)
