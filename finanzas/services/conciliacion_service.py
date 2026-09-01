from decimal import Decimal

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from finanzas.models import (
    ConciliacionBancaria,
    ConciliacionDetalle,
    CuentaBancaria,
    MovimientoBancario,
)


class ConciliacionService:
    @staticmethod
    @transaction.atomic
    def preparar_conciliacion(
        *,
        empresa,
        cuenta_bancaria_id: int,
        fecha_inicio,
        fecha_final,
        saldo_estado_cuenta,
    ):
        cuenta = CuentaBancaria.objects.filter(pk=cuenta_bancaria_id).first()
        if cuenta is None:
            raise ValidationError(
                {"cuenta_bancaria": "Cuenta bancaria no encontrada."}
            )
        if cuenta.empresa_id and empresa and cuenta.empresa_id != getattr(empresa, "pk", empresa):
            raise ValidationError(
                {"cuenta_bancaria": "Cuenta bancaria no pertenece a la empresa."}
            )
        if fecha_inicio and fecha_final and fecha_inicio > fecha_final:
            raise ValidationError(
                {"fecha_inicio": "La fecha inicial no puede ser mayor a la final."}
            )

        qs = MovimientoBancario.objects.filter(cuenta_bancaria=cuenta)
        if fecha_inicio:
            qs = qs.filter(fecha__gte=fecha_inicio)
        if fecha_final:
            qs = qs.filter(fecha__lte=fecha_final)
        movimientos = list(
            qs.exclude(estatus=MovimientoBancario.Estatus.CANCELADO)
            .select_related("cobro", "pago")
            .order_by("fecha", "id")
            .all()
        )
        total_abonos = sum(
            (
                Decimal(str(m.importe or 0))
                for m in movimientos
                if m.tipo_movimiento == MovimientoBancario.TipoMovimiento.ABONO
            ),
            Decimal("0.00"),
        )
        total_cargos = sum(
            (
                Decimal(str(m.importe or 0))
                for m in movimientos
                if m.tipo_movimiento == MovimientoBancario.TipoMovimiento.CARGO
            ),
            Decimal("0.00"),
        )
        saldo_inicial_libros = Decimal(str(cuenta.saldo_actual or 0)) - total_abonos + total_cargos
        saldo_libros = (saldo_inicial_libros + total_abonos - total_cargos).quantize(
            Decimal("0.01")
        )
        diferencia = (
            Decimal(str(saldo_estado_cuenta or 0)) - saldo_libros
        ).quantize(Decimal("0.01"))

        conciliacion = ConciliacionBancaria.objects.create(
            cuenta_bancaria=cuenta,
            fecha_inicio=fecha_inicio,
            fecha_final=fecha_final,
            saldo_estado_cuenta=Decimal(str(saldo_estado_cuenta or 0)).quantize(
                Decimal("0.01")
            ),
            saldo_libros=saldo_libros,
            estatus=ConciliacionBancaria.Estatus.BORRADOR,
        )
        for mov in movimientos:
            ConciliacionDetalle.objects.get_or_create(
                conciliacion=conciliacion, movimiento_bancario=mov
            )

        movimientos_pendientes = [
            {
                "id": m.id,
                "fecha": m.fecha,
                "concepto": m.concepto,
                "referencia": m.referencia,
                "tipo_movimiento": m.tipo_movimiento,
                "importe": str(m.importe),
                "estatus": m.estatus,
                "origen": m.origen,
                "cobro_id": m.cobro_id,
                "pago_id": m.pago_id,
            }
            for m in movimientos
        ]
        return {
            "id": conciliacion.pk,
            "cuenta_bancaria": {
                "id": cuenta.pk,
                "alias": cuenta.alias,
                "banco": getattr(cuenta.banco, "nombre", None),
            },
            "fecha_inicio": conciliacion.fecha_inicio,
            "fecha_final": conciliacion.fecha_final,
            "saldo_estado_cuenta": str(conciliacion.saldo_estado_cuenta),
            "saldo_libros": str(saldo_libros),
            "diferencia": str(diferencia),
            "total_abonos_rango": str(total_abonos.quantize(Decimal("0.01"))),
            "total_cargos_rango": str(total_cargos.quantize(Decimal("0.01"))),
            "estatus": conciliacion.estatus,
            "movimientos_pendientes": movimientos_pendientes,
        }

    @staticmethod
    @transaction.atomic
    def cerrar_conciliacion(conciliacion: ConciliacionBancaria):
        if conciliacion.estatus == ConciliacionBancaria.Estatus.CERRADA:
            return conciliacion
        if conciliacion.estatus == ConciliacionBancaria.Estatus.CANCELADA:
            raise ValidationError(
                {"estatus": "No se puede cerrar una conciliación cancelada."}
            )
        diferencia = conciliacion.saldo_estado_cuenta - conciliacion.saldo_libros
        if abs(diferencia) > Decimal("0.01"):
            raise ValidationError(
                {
                    "diferencia": (
                        f"La diferencia ({diferencia}) debe ser 0.00 para cerrar "
                        "la conciliación."
                    )
                }
            )
        detalles = list(
            ConciliacionDetalle.objects.filter(conciliacion=conciliacion).all()
        )
        if detalles:
            mov_ids = [d.movimiento_bancario_id for d in detalles]
            MovimientoBancario.objects.filter(pk__in=mov_ids).update(
                estatus=MovimientoBancario.Estatus.CONCILIADO
            )
        conciliacion.estatus = ConciliacionBancaria.Estatus.CERRADA
        conciliacion.save(update_fields=["estatus", "updated_at"])
        return conciliacion
