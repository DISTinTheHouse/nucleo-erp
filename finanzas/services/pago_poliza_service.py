"""Asiento de un pago: cargo a Proveedores, abono al banco."""

from finanzas.models import ConceptoContable, Poliza
from finanzas.services.plan_contable_service import PlanContableService
from finanzas.services.poliza_motor_service import PolizaMotorService

ORIGEN = "Pago"


class PagoPolizaService:
    @staticmethod
    def generar(pago, usuario=None):
        """Póliza del pago, o ``None`` si la empresa no contabiliza."""
        if not PlanContableService.contabilizacion_activa(pago.empresa_id, fecha=pago.fecha_pago):
            return None
        referencia = pago.referencia_operacion or str(pago.pk)
        return PolizaMotorService.generar(
            empresa_id=pago.empresa_id,
            # EC-874: la sucursal del pago (la de sus facturas de proveedor).
            sucursal_id=PolizaMotorService.sucursal_id(pago.empresa_id, pago.sucursal_id, usuario),
            origen_tipo=ORIGEN,
            origen_id=pago.pk,
            fecha=pago.fecha_pago,
            tipo=Poliza.PolizaTipo.EGRESO,
            concepto=f"Pago {pago.pk} - {pago.proveedor.nombre}",
            lineas=[
                {"concepto": ConceptoContable.PROVEEDORES,
                 "cargo": pago.total_pagado, "pago": pago, "referencia": referencia},
                {"cuenta": PlanContableService.cuenta_de_banco(pago.cuenta_bancaria),
                 "abono": pago.total_pagado, "pago": pago, "referencia": referencia},
            ],
            usuario=usuario,
        )

    @staticmethod
    def cancelar(pago, usuario=None):
        return PolizaMotorService.cancelar_de(ORIGEN, pago.pk, usuario, empresa_id=pago.empresa_id)
