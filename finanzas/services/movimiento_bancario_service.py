from decimal import Decimal

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from finanzas.models import CuentaBancaria, MovimientoBancario


class MovimientoBancarioService:
    @staticmethod
    def _validar_empresa(cuenta: CuentaBancaria, empresa_id=None):
        if empresa_id and cuenta.empresa_id and cuenta.empresa_id != empresa_id:
            raise ValidationError(
                {"cuenta_bancaria": "Cuenta bancaria no pertenece a la empresa."}
            )

    @staticmethod
    @transaction.atomic
    def registrar_movimiento(movimiento: MovimientoBancario, empresa_id=None):
        if movimiento.cobro_id or movimiento.pago_id:
            return
        cuenta = CuentaBancaria.objects.select_for_update().get(
            pk=movimiento.cuenta_bancaria_id
        )
        MovimientoBancarioService._validar_empresa(cuenta, empresa_id)

        importe = Decimal(str(movimiento.importe or 0))
        if importe <= 0:
            raise ValidationError({"importe": "El importe debe ser mayor a 0."})

        if movimiento.tipo_movimiento == MovimientoBancario.TipoMovimiento.CARGO:
            nuevo_saldo = (Decimal(str(cuenta.saldo_actual or 0)) - importe).quantize(
                Decimal("0.01")
            )
        else:
            nuevo_saldo = (Decimal(str(cuenta.saldo_actual or 0)) + importe).quantize(
                Decimal("0.01")
            )
        movimiento.saldo = nuevo_saldo
        cuenta.saldo_actual = nuevo_saldo
        cuenta.save()

    @staticmethod
    @transaction.atomic
    def revertir_movimiento(movimiento: MovimientoBancario):
        if movimiento.estatus == MovimientoBancario.Estatus.CANCELADO:
            return
        if movimiento.cobro_id or movimiento.pago_id:
            raise ValidationError(
                {
                    "movimiento_bancario": (
                        "Los movimientos generados por cobros o pagos se "
                        "cancelan desde su documento origen."
                    )
                }
            )
        cuenta = CuentaBancaria.objects.select_for_update().get(
            pk=movimiento.cuenta_bancaria_id
        )
        importe = Decimal(str(movimiento.importe or 0))
        if movimiento.tipo_movimiento == MovimientoBancario.TipoMovimiento.CARGO:
            cuenta.saldo_actual = (
                Decimal(str(cuenta.saldo_actual or 0)) + importe
            ).quantize(Decimal("0.01"))
        else:
            cuenta.saldo_actual = (
                Decimal(str(cuenta.saldo_actual or 0)) - importe
            ).quantize(Decimal("0.01"))
        cuenta.save()
        movimiento.estatus = MovimientoBancario.Estatus.CANCELADO
        movimiento.save()

    @staticmethod
    def resumen_cuenta(cuenta: CuentaBancaria):
        hoy = timezone.localdate()
        mes_inicio = hoy.replace(day=1)

        cargos_mes = MovimientoBancario.objects.filter(
            cuenta_bancaria=cuenta,
            fecha__gte=mes_inicio,
            fecha__lte=hoy,
            tipo_movimiento=MovimientoBancario.TipoMovimiento.CARGO,
            estatus=MovimientoBancario.Estatus.CONCILIADO
            if False
            else MovimientoBancario.Estatus.PENDIENTE,
        )
        abonos_mes = MovimientoBancario.objects.filter(
            cuenta_bancaria=cuenta,
            fecha__gte=mes_inicio,
            fecha__lte=hoy,
            tipo_movimiento=MovimientoBancario.TipoMovimiento.ABONO,
        )
        total_cargos = sum(
            (Decimal(str(m.importe or 0)) for m in cargos_mes), Decimal("0.00")
        )
        total_abonos = sum(
            (Decimal(str(m.importe or 0)) for m in abonos_mes), Decimal("0.00")
        )
        ultimos = list(
            MovimientoBancario.objects.filter(cuenta_bancaria=cuenta)
            .select_related("cobro", "pago")
            .order_by("-fecha", "-id")[:10]
        )
        ultimos_data = [
            {
                "id": m.id,
                "fecha": m.fecha,
                "concepto": m.concepto,
                "referencia": m.referencia,
                "tipo_movimiento": m.tipo_movimiento,
                "importe": str(m.importe),
                "saldo": str(m.saldo),
                "estatus": m.estatus,
                "origen": m.origen,
                "cobro_id": m.cobro_id,
                "pago_id": m.pago_id,
            }
            for m in ultimos
        ]
        return {
            "id": cuenta.pk,
            "alias": cuenta.alias,
            "numero_cuenta": cuenta.numero_cuenta,
            "banco": getattr(cuenta.banco, "nombre", None),
            "moneda": getattr(cuenta.moneda, "codigo_iso", None),
            "saldo_actual": str(cuenta.saldo_actual),
            "total_cargos_mes": str(total_cargos.quantize(Decimal("0.01"))),
            "total_abonos_mes": str(total_abonos.quantize(Decimal("0.01"))),
            "ultimos_movimientos": ultimos_data,
        }
