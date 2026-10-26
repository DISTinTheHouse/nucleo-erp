"""Asiento de una factura de venta: cargo a Clientes, abono a Ventas e IVA trasladado."""

from decimal import Decimal

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import ConceptoContable, Poliza
from finanzas.services.poliza_motor_service import PolizaMotorService
from finanzas.utils.moneda import a_mxn

ORIGEN = "Factura"


def tipo_cambio_de(factura):
    """Pesos por unidad de la factura; sin él no hay asiento en pesos (antes iba a 1:1, #668)."""
    tc = factura.tipo_cambio_mxn()
    if tc is None:
        raise ErrorDeNegocio(
            {"tipo_cambio": f"La factura está en {factura.moneda.codigo_iso} y no tiene tipo de cambio."}
        )
    return tc


class FacturaPolizaService:
    @staticmethod
    def lineas(factura):
        folio = factura.folio or str(factura.pk)
        referencia = folio
        neto = (factura.subtotal or Decimal("0")) - (factura.descuento or Decimal("0"))
        # En pesos (EC-581): fuera de MXN, el ingreso se queda con el redondeo para que cuadre.
        tc = tipo_cambio_de(factura)
        total, impuestos = a_mxn(factura.total, tc), a_mxn(factura.impuestos, tc)
        neto = neto if tc == 1 else total - impuestos
        return [
            {
                "concepto": ConceptoContable.CLIENTES,
                "cargo": total,
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
                "abono": impuestos,
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

    @staticmethod
    def cancelar(factura, usuario=None):
        """Contrapóliza de la factura, fechada hoy. Sin póliza no hace nada."""
        return PolizaMotorService.cancelar_de(ORIGEN, factura.pk, usuario)
