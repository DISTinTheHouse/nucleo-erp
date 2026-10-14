"""Asiento de un pago: cargo a Proveedores, abono al banco."""

from finanzas.models import ConceptoContable, PagoDetalle, Poliza
from finanzas.services.plan_contable_service import PlanContableService
from finanzas.services.poliza_motor_service import PolizaMotorService

ORIGEN = "Pago"


class PagoPolizaService:
    @staticmethod
    def generar(pago, usuario=None):
        """Póliza del pago, o ``None`` si la empresa no contabiliza."""
        if not PlanContableService.contabilizacion_activa(pago.empresa_id, fecha=pago.fecha_pago):
            return None
        sucursal_factura = (
            PagoDetalle.objects.filter(pago=pago)
            .values_list("cxp__factura_proveedor__sucursal_id", flat=True).first()
        )
        referencia = pago.referencia_operacion or str(pago.pk)
        return PolizaMotorService.generar(
            empresa_id=pago.empresa_id,
            sucursal_id=PolizaMotorService.sucursal_id(pago.empresa_id, sucursal_factura, usuario),
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
        return PolizaMotorService.cancelar_de(ORIGEN, pago.pk, usuario)
