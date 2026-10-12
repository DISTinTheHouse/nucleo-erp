"""Asiento de un movimiento bancario sin documento (comisión, depósito suelto)."""

from finanzas.models import ConceptoContable, MovimientoBancario, Poliza
from finanzas.services.plan_contable_service import PlanContableService
from finanzas.services.poliza_motor_service import PolizaMotorService

ORIGEN = "MovimientoBancario"


class MovimientoBancarioPolizaService:
    @staticmethod
    def generar(movimiento, usuario=None):
        """Póliza del movimiento, o ``None`` si la empresa no contabiliza."""
        cuenta = movimiento.cuenta_bancaria
        if not PlanContableService.contabilizacion_activa(cuenta.empresa_id, fecha=movimiento.fecha):
            return None
        retiro = movimiento.tipo_movimiento == MovimientoBancario.TipoMovimiento.CARGO
        banco = {"cuenta": PlanContableService.cuenta_de_banco(cuenta)}
        if movimiento.cuenta_contable_contrapartida_id:
            contrapartida = {"cuenta": movimiento.cuenta_contable_contrapartida}
        else:
            contrapartida = {"concepto": (
                ConceptoContable.CONTRAPARTIDA_MB_CARGO if retiro
                else ConceptoContable.CONTRAPARTIDA_MB_ABONO
            )}
        referencia = movimiento.referencia or str(movimiento.pk)
        importe = movimiento.importe
        lado_banco, lado_contrapartida = ("abono", "cargo") if retiro else ("cargo", "abono")
        return PolizaMotorService.generar(
            empresa_id=cuenta.empresa_id,
            sucursal_id=PolizaMotorService.sucursal_id(cuenta.empresa_id, None, usuario),
            origen_tipo=ORIGEN,
            origen_id=movimiento.pk,
            fecha=movimiento.fecha,
            tipo=Poliza.PolizaTipo.EGRESO if retiro else Poliza.PolizaTipo.INGRESO,
            concepto=(movimiento.concepto or f"Movimiento bancario {movimiento.pk}"),
            lineas=[
                {**banco, lado_banco: importe, "movimiento_bancario": movimiento, "referencia": referencia},
                {**contrapartida, lado_contrapartida: importe, "movimiento_bancario": movimiento,
                 "referencia": referencia},
            ],
            usuario=usuario,
        )

    @staticmethod
    def cancelar(movimiento, usuario=None):
        return PolizaMotorService.cancelar_de(ORIGEN, movimiento.pk, usuario)
