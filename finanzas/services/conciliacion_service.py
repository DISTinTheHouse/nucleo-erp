from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from finanzas.exceptions import ErrorDeNegocio
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
            raise ErrorDeNegocio(
                {"cuenta_bancaria": "Cuenta bancaria no encontrada."}
            )
        if cuenta.empresa_id and empresa and cuenta.empresa_id != getattr(empresa, "pk", empresa):
            raise ErrorDeNegocio(
                {"cuenta_bancaria": "Cuenta bancaria no pertenece a la empresa."}
            )
        if fecha_inicio and fecha_final and fecha_inicio > fecha_final:
            raise ErrorDeNegocio(
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
        saldo_libros = ConciliacionService._saldo_libros_al_cierre(cuenta, fecha_final)
        diferencia = (
            Decimal(str(saldo_estado_cuenta or 0)) - saldo_libros
        ).quantize(Decimal("0.01"))

        saldo_estado_cuenta = Decimal(str(saldo_estado_cuenta or 0)).quantize(
            Decimal("0.01")
        )
        # Preparar dos veces el mismo periodo reescribe el borrador en vez de
        # dejar otra conciliación colgando: antes cada llamada creaba una nueva,
        # con sus propias líneas, y el mismo movimiento acababa ligado a varias.
        # Sólo se reutiliza el borrador: una cerrada o cancelada ya no se toca.
        conciliacion = (
            ConciliacionBancaria.objects.filter(
                cuenta_bancaria=cuenta,
                fecha_inicio=fecha_inicio,
                fecha_final=fecha_final,
                estatus=ConciliacionBancaria.Estatus.BORRADOR,
            )
            .order_by("id")
            .first()
        )
        if conciliacion is None:
            conciliacion = ConciliacionBancaria.objects.create(
                cuenta_bancaria=cuenta,
                fecha_inicio=fecha_inicio,
                fecha_final=fecha_final,
                saldo_estado_cuenta=saldo_estado_cuenta,
                saldo_libros=saldo_libros,
                estatus=ConciliacionBancaria.Estatus.BORRADOR,
            )
        else:
            conciliacion.saldo_estado_cuenta = saldo_estado_cuenta
            conciliacion.saldo_libros = saldo_libros
            conciliacion.save(
                update_fields=["saldo_estado_cuenta", "saldo_libros", "updated_at"]
            )
            # Las líneas se ponen al día con los movimientos de ahora: las que
            # sobran se van --un movimiento cancelado entretanto-- y las que
            # siguen conservan sus ``observaciones``.
            ConciliacionDetalle.objects.filter(conciliacion=conciliacion).exclude(
                movimiento_bancario__in=movimientos
            ).delete()
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
    def _saldo_libros_al_cierre(cuenta: CuentaBancaria, fecha_final):
        """Saldo de libros de la cuenta al cierre de ``fecha_final``.

        La cuenta sólo guarda su saldo vivo, así que el saldo histórico se
        reconstruye hacia atrás: se parte de ``saldo_actual`` y se deshace cada
        movimiento posterior al cierre --se resta lo abonado y se devuelve lo
        cargado--, que es la inversa de cómo lo aplicaron ``CobroService``,
        ``PagoService`` y ``MovimientoBancarioService``.

        Los cancelados quedan fuera, igual que en los totales del rango: su
        efecto ya se revirtió en ``saldo_actual`` cuando se cancelaron. No se usa
        ``MovimientoBancario.saldo``: ese campo no se mantiene en la vía manual.

        Sin ``fecha_final`` no hay cierre que reconstruir y el saldo de libros es
        el vivo.
        """
        saldo = Decimal(str(cuenta.saldo_actual or 0))
        if fecha_final is None:
            return saldo.quantize(Decimal("0.01"))
        # Los dos totales salen en una sola consulta: recorrer fila por fila
        # costaba toda la historia posterior al cierre, que no está acotada por
        # el rango pedido.
        cero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=18, decimal_places=2))
        posteriores = (
            MovimientoBancario.objects.filter(
                cuenta_bancaria=cuenta, fecha__gt=fecha_final
            )
            .exclude(estatus=MovimientoBancario.Estatus.CANCELADO)
            .aggregate(
                abonos=Coalesce(
                    Sum(
                        "importe",
                        filter=Q(
                            tipo_movimiento=MovimientoBancario.TipoMovimiento.ABONO
                        ),
                    ),
                    cero,
                ),
                cargos=Coalesce(
                    Sum(
                        "importe",
                        filter=Q(
                            tipo_movimiento=MovimientoBancario.TipoMovimiento.CARGO
                        ),
                    ),
                    cero,
                ),
            )
        )
        saldo = (
            saldo
            - Decimal(str(posteriores["abonos"]))
            + Decimal(str(posteriores["cargos"]))
        )
        return saldo.quantize(Decimal("0.01"))

    @staticmethod
    @transaction.atomic
    def cerrar_conciliacion(conciliacion: ConciliacionBancaria):
        if conciliacion.estatus == ConciliacionBancaria.Estatus.CERRADA:
            return conciliacion
        if conciliacion.estatus == ConciliacionBancaria.Estatus.CANCELADA:
            raise ErrorDeNegocio(
                {"estatus": "No se puede cerrar una conciliación cancelada."}
            )
        diferencia = conciliacion.saldo_estado_cuenta - conciliacion.saldo_libros
        if abs(diferencia) > Decimal("0.01"):
            raise ErrorDeNegocio(
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
