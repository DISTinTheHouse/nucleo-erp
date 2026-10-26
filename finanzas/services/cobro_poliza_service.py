"""Asiento de un cobro: cargo al banco, abono a Clientes."""

from finanzas.models import CobroDetalle, ConceptoContable, Poliza
from finanzas.services.plan_contable_service import PlanContableService
from finanzas.services.poliza_motor_service import PolizaMotorService
from finanzas.utils.moneda import a_mxn

ORIGEN = "Cobro"


class CobroPolizaService:
    @staticmethod
    def generar(cobro, usuario=None):
        """Póliza del cobro, o ``None`` si la empresa no contabiliza."""
        if not PlanContableService.contabilizacion_activa(cobro.empresa_id, fecha=cobro.fecha_cobro):
            return None
        sucursal_factura = (
            CobroDetalle.objects.filter(cobro=cobro)
            .values_list("cxc__factura__sucursal_id", flat=True).first()
        )
        referencia = cobro.referencia_operacion or str(cobro.pk)
        # En pesos con el tipo de cambio del cobro (MXN = 1). La diferencia contra el de la
        # factura (fluctuación cambiaria) queda para la fase 2 de EC-581.
        importe = a_mxn(cobro.total_cobrado, cobro.tipo_cambio)
        return PolizaMotorService.generar(
            empresa_id=cobro.empresa_id,
            sucursal_id=PolizaMotorService.sucursal_id(cobro.empresa_id, sucursal_factura, usuario),
            origen_tipo=ORIGEN,
            origen_id=cobro.pk,
            fecha=cobro.fecha_cobro,
            tipo=Poliza.PolizaTipo.INGRESO,
            concepto=f"Cobro {cobro.folio or cobro.pk} - {cobro.cliente.nombre}",
            lineas=[
                {"cuenta": PlanContableService.cuenta_de_banco(cobro.cuenta_bancaria),
                 "cargo": importe, "cobro": cobro, "referencia": referencia},
                {"concepto": ConceptoContable.CLIENTES,
                 "abono": importe, "cobro": cobro, "referencia": referencia},
            ],
            usuario=usuario,
        )

    @staticmethod
    def cancelar(cobro, usuario=None):
        return PolizaMotorService.cancelar_de(ORIGEN, cobro.pk, usuario)
