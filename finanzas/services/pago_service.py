from decimal import Decimal

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone

from finanzas.models import (
    CuentaBancaria,
    CuentaPorPagar,
    MovimientoBancario,
    Pago,
    PagoDetalle,
)


class PagoService:
    @staticmethod
    @transaction.atomic
    def aplicar_pago(pago: Pago):
        detalles = list(
            PagoDetalle.objects.select_for_update()
            .select_related("cxp")
            .filter(pago=pago)
        )
        if not detalles:
            raise ValidationError(
                {"pago_detalles": "El pago debe tener al menos un detalle."}
            )

        cxp_ids = [d.cxp_id for d in detalles]
        cxps = {
            cxp.pk: cxp
            for cxp in CuentaPorPagar.objects.select_for_update()
            .filter(pk__in=cxp_ids)
            .all()
        }

        suma_aplicado = Decimal("0.00")
        for det in detalles:
            cxp = cxps.get(det.cxp_id)
            if cxp is None:
                raise ValidationError(
                    {"pago_detalles": f"CxP {det.cxp_id} no encontrada."}
                )
            if cxp.empresa_id and pago.empresa_id and cxp.empresa_id != pago.empresa_id:
                raise ValidationError(
                    {"pago_detalles": "CxP no pertenece a la misma empresa que el pago."}
                )
            importe = Decimal(str(det.importe_aplicado or 0))
            if importe <= 0:
                raise ValidationError(
                    {"pago_detalles": "Cada importe aplicado debe ser mayor a 0."}
                )
            saldo_actual = Decimal(str(cxp.saldo or 0))
            if importe > saldo_actual + Decimal("0.0001"):
                raise ValidationError(
                    {
                        "pago_detalles": (
                            f"Importe {importe} excede saldo {saldo_actual} "
                            f"de la CxP {cxp.pk}."
                        )
                    }
                )
            suma_aplicado += importe

        total_pagado = Decimal(str(pago.total_pagado or 0))
        if abs(suma_aplicado - total_pagado) > Decimal("0.01"):
            raise ValidationError(
                {
                    "total_pagado": (
                        f"La suma de importes aplicados ({suma_aplicado}) "
                        f"debe coincidir con total_pagado ({total_pagado})."
                    )
                }
            )

        hoy = timezone.localdate()
        for det in detalles:
            cxp = cxps[det.cxp_id]
            importe = Decimal(str(det.importe_aplicado or 0))
            cxp.saldo = (Decimal(str(cxp.saldo or 0)) - importe).quantize(Decimal("0.01"))
            cxp.fecha_ultimo_pago = hoy
            if cxp.saldo <= Decimal("0.00"):
                cxp.saldo = Decimal("0.00")
                cxp.estatus = CuentaPorPagar.EstatusCxP.PAGADA
            else:
                cxp.estatus = CuentaPorPagar.EstatusCxP.PARCIAL
            cxp.save()

        cuenta = CuentaBancaria.objects.select_for_update().get(pk=pago.cuenta_bancaria_id)
        if pago.empresa_id and cuenta.empresa_id and cuenta.empresa_id != pago.empresa_id:
            raise ValidationError(
                {"cuenta_bancaria": "Cuenta bancaria no pertenece a la empresa."}
            )
        saldo_anterior = Decimal(str(cuenta.saldo_actual or 0))
        cuenta.saldo_actual = (saldo_anterior - total_pagado).quantize(Decimal("0.01"))
        cuenta.save()

        mb = MovimientoBancario.objects.filter(pago=pago).first()
        if mb is None:
            MovimientoBancario.objects.create(
                cuenta_bancaria=cuenta,
                pago=pago,
                fecha=pago.fecha_pago or hoy,
                concepto=(f"Pago {pago.pk} - Proveedor {pago.proveedor_id}")[:255],
                referencia=(pago.referencia_operacion or str(pago.pk))[:100],
                importe=total_pagado,
                saldo=cuenta.saldo_actual,
                origen=MovimientoBancario.OrigenOpciones.MANUAL,
                tipo_movimiento=MovimientoBancario.TipoMovimiento.CARGO,
                estatus=MovimientoBancario.Estatus.PENDIENTE,
            )
        else:
            mb.importe = total_pagado
            mb.saldo = cuenta.saldo_actual
            mb.fecha = pago.fecha_pago or hoy
            mb.tipo_movimiento = MovimientoBancario.TipoMovimiento.CARGO
            mb.save()

    @staticmethod
    @transaction.atomic
    def cancelar_pago(pago: Pago):
        if pago.estatus == Pago.Estatus.CANCELADO:
            return
        detalles = list(PagoDetalle.objects.filter(pago=pago).select_related("cxp").all())
        cxp_ids = [d.cxp_id for d in detalles]
        cxps = {
            cxp.pk: cxp
            for cxp in CuentaPorPagar.objects.select_for_update().filter(pk__in=cxp_ids)
        }
        for det in detalles:
            cxp = cxps.get(det.cxp_id)
            if cxp is None:
                continue
            importe = Decimal(str(det.importe_aplicado or 0))
            cxp.saldo = (Decimal(str(cxp.saldo or 0)) + importe).quantize(Decimal("0.01"))
            if cxp.saldo >= Decimal(str(cxp.total or 0)) - Decimal("0.01"):
                cxp.estatus = CuentaPorPagar.EstatusCxP.PENDIENTE
            else:
                cxp.estatus = CuentaPorPagar.EstatusCxP.PARCIAL
            cxp.save()

        total_pagado = Decimal(str(pago.total_pagado or 0))
        cuenta = CuentaBancaria.objects.select_for_update().get(pk=pago.cuenta_bancaria_id)
        cuenta.saldo_actual = (
            Decimal(str(cuenta.saldo_actual or 0)) + total_pagado
        ).quantize(Decimal("0.01"))
        cuenta.save()

        pago.estatus = Pago.Estatus.CANCELADO
        pago.save()

        mb = MovimientoBancario.objects.filter(pago=pago).first()
        if mb:
            mb.estatus = MovimientoBancario.Estatus.CANCELADO
            mb.save()
