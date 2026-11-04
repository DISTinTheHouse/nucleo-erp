"""Asiento de una nota de crédito: devolución e IVA contra Clientes."""

from django.utils import timezone

from decimal import Decimal

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import ConceptoContable, Poliza
from finanzas.services.factura_poliza_service import tipo_cambio_de
from finanzas.services.plan_contable_service import PlanContableService
from finanzas.services.poliza_motor_service import PolizaMotorService
from finanzas.services.poliza_service import CENTAVO
from finanzas.utils.moneda import a_mxn

ORIGEN = "NotaCredito"


def _lineas(nota):
    """Asiento en pesos, con el tipo de cambio de su factura (EC-581). Solo se arma si
    la empresa contabiliza: sin póliza no se exige tipo de cambio (#676)."""
    factura = nota.factura
    folio = nota.folio or str(nota.pk)
    tc = tipo_cambio_de(factura)
    total, impuestos = a_mxn(nota.total, tc), a_mxn(nota.impuestos, tc)
    subtotal = nota.subtotal if tc == 1 else total - impuestos
    return [
        {"concepto": ConceptoContable.DEVOLUCIONES_VENTAS, "cargo": subtotal,
         "referencia": folio},
        {"concepto": ConceptoContable.IVA_TRASLADADO, "cargo": impuestos,
         "referencia": folio},
        {"concepto": ConceptoContable.CLIENTES, "abono": total,
         "factura": factura, "referencia": folio},
    ]


class NotaCreditoPolizaService:
    @staticmethod
    def validar(nota):
        """Lo que ``generar`` revisaría (cuentas configuradas y cuadre), sin guardar.

        Se llama antes de timbrar: un fallo contable después del timbre dejaba la
        nota timbrada y aplicada sin póliza y sin forma de generarla (#749).
        """
        factura = nota.factura
        fecha = nota.fecha_emision or timezone.localdate()
        if not PlanContableService.contabilizacion_activa(factura.empresa_id, fecha=fecha):
            return
        resueltas, _ = PolizaMotorService.resolver(factura.empresa_id, _lineas(nota))
        cargos = sum((r[3] for r in resueltas), Decimal("0.00"))
        abonos = sum((r[4] for r in resueltas), Decimal("0.00"))
        if abs(cargos - abonos) > CENTAVO:
            raise ErrorDeNegocio({"poliza": f"La póliza de la nota no cuadra: cargos {cargos}, abonos {abonos}."})

    @staticmethod
    def generar(nota, usuario=None):
        """Póliza de la nota, o ``None`` si la empresa no contabiliza."""
        factura = nota.factura
        folio = nota.folio or str(nota.pk)
        return PolizaMotorService.generar(
            empresa_id=factura.empresa_id,
            sucursal_id=factura.sucursal_id,
            origen_tipo=ORIGEN,
            origen_id=nota.pk,
            fecha=nota.fecha_emision or timezone.localdate(),
            tipo=Poliza.PolizaTipo.DIARIO,
            concepto=f"Nota de crédito {folio} - {nota.cliente.nombre}",
            lineas=lambda: _lineas(nota),
            usuario=usuario,
        )

    @staticmethod
    def cancelar(nota, usuario=None):
        return PolizaMotorService.cancelar_de(ORIGEN, nota.pk, usuario, empresa_id=nota.factura.empresa_id)
