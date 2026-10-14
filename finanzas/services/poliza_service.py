from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Poliza, PolizaDetalle
from finanzas.utils.folios import generate_poliza_folio

CENTAVO = Decimal("0.01")

# FKs de documento que la contrapóliza copia tal cual a cada renglón.
_FK_DOCUMENTO = ("factura_id", "factura_proveedor_id", "pago_id", "cobro_id", "movimiento_bancario_id")


class PolizaService:
    @staticmethod
    def calcular_sumas(poliza: Poliza, detalles=None):
        """``(cargos, abonos)``. Con ``detalles`` en memoria no consulta la base."""
        if detalles is None:
            detalles = list(PolizaDetalle.objects.filter(poliza=poliza))
        cargos = sum((Decimal(str(d.cargo or 0)) for d in detalles), Decimal("0.00"))
        abonos = sum((Decimal(str(d.abono or 0)) for d in detalles), Decimal("0.00"))
        return cargos.quantize(CENTAVO), abonos.quantize(CENTAVO)

    @staticmethod
    def validar_suma_cero(poliza: Poliza, detalles=None):
        cargos, abonos = PolizaService.calcular_sumas(poliza, detalles)
        if abs(cargos - abonos) > CENTAVO:
            raise ErrorDeNegocio(
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
            raise ErrorDeNegocio(
                {"estatus": "No se puede contabilizar una póliza cancelada."}
            )
        PolizaService.validar_suma_cero(poliza)
        poliza.estatus = Poliza.PolizaStatus.CONTABILIZADA
        poliza.save(update_fields=["estatus", "updated_at"])

    @staticmethod
    @transaction.atomic
    def contrapoliza(poliza: Poliza, usuario=None):
        """Póliza con cargos y abonos invertidos, fechada hoy. La original queda Cancelada.

        Si ya tiene contrapóliza, regresa esa: cancelar dos veces no duplica nada.
        """
        original = Poliza.objects.select_for_update().get(pk=poliza.pk)
        existente = Poliza.objects.filter(poliza_reversa_de=original).first()
        if existente:
            return existente
        if original.estatus != Poliza.PolizaStatus.CONTABILIZADA:
            raise ErrorDeNegocio(
                {"estatus": "Solo una póliza contabilizada tiene contrapóliza."}
            )

        folio, consecutivo = generate_poliza_folio(original.empresa_id, original.sucursal_id)
        reversa = Poliza.objects.create(
            empresa_id=original.empresa_id,
            sucursal_id=original.sucursal_id,
            centro_costo_id=original.centro_costo_id,
            folio=folio,
            folio_consecutivo=consecutivo,
            tipo=original.tipo,
            fecha=timezone.localdate(),
            concepto=f"Cancelación de {original.folio or original.pk}: {original.concepto or ''}"[:200],
            estatus=Poliza.PolizaStatus.CONTABILIZADA,
            usuario_creacion=usuario or original.usuario_creacion,
            poliza_reversa_de=original,
            origen_tipo=original.origen_tipo,
            origen_id=original.origen_id,
        )
        PolizaDetalle.objects.bulk_create(
            PolizaDetalle(
                poliza=reversa,
                cuenta_contable_id=d.cuenta_contable_id,
                centro_costo_id=d.centro_costo_id,
                cargo=d.abono,
                abono=d.cargo,
                referencia=d.referencia,
                observaciones=d.observaciones,
                orden=d.orden,
                **{campo: getattr(d, campo) for campo in _FK_DOCUMENTO},
            )
            for d in original.poliza_detalles.all()
        )
        original.estatus = Poliza.PolizaStatus.CANCELADA
        original.save(update_fields=["estatus", "updated_at"])
        return reversa

    @staticmethod
    @transaction.atomic
    def cancelar(poliza: Poliza, usuario=None):
        """Borrador: solo cambia el estatus. Contabilizada: genera su contrapóliza.

        Regresa la contrapóliza, o ``None`` si no hubo.
        """
        if poliza.estatus == Poliza.PolizaStatus.CANCELADA:
            return Poliza.objects.filter(poliza_reversa_de=poliza).first()
        if poliza.estatus == Poliza.PolizaStatus.BORRADOR:
            poliza.estatus = Poliza.PolizaStatus.CANCELADA
            poliza.save(update_fields=["estatus", "updated_at"])
            return None
        return PolizaService.contrapoliza(poliza, usuario)
