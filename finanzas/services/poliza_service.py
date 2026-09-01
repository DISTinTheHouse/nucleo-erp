from decimal import Decimal

from django.db import transaction
from django.core.exceptions import ValidationError

from finanzas.models import Poliza, PolizaDetalle


class PolizaService:
    @staticmethod
    def calcular_sumas(poliza: Poliza):
        detalles = list(
            PolizaDetalle.objects.filter(poliza=poliza).all()
        )
        total_cargos = sum(
            (Decimal(str(d.cargo or 0)) for d in detalles), Decimal("0.00")
        )
        total_abonos = sum(
            (Decimal(str(d.abono or 0)) for d in detalles), Decimal("0.00")
        )
        return total_cargos.quantize(Decimal("0.01")), total_abonos.quantize(Decimal("0.01"))

    @staticmethod
    def validar_suma_cero(poliza: Poliza):
        cargos, abonos = PolizaService.calcular_sumas(poliza)
        if abs(cargos - abonos) > Decimal("0.01"):
            raise ValidationError(
                {
                    "poliza_detalles": (
                        f"La suma de cargos ({cargos}) debe ser igual a la "
                        f"suma de abonos ({abonos})."
                    )
                }
            )
        return cargos, abonos

    @staticmethod
    @transaction.atomic
    def contabilizar(poliza: Poliza):
        if poliza.estatus == Poliza.PolizaStatus.CONTABILIZADA:
            return
        if poliza.estatus == Poliza.PolizaStatus.CANCELADA:
            raise ValidationError(
                {"estatus": "No se puede contabilizar una póliza cancelada."}
            )
        PolizaService.validar_suma_cero(poliza)
        poliza.estatus = Poliza.PolizaStatus.CONTABILIZADA
        poliza.save(update_fields=["estatus", "updated_at"])

    @staticmethod
    @transaction.atomic
    def cancelar(poliza: Poliza):
        if poliza.estatus == Poliza.PolizaStatus.CANCELADA:
            return
        poliza.estatus = Poliza.PolizaStatus.CANCELADA
        poliza.save(update_fields=["estatus", "updated_at"])
