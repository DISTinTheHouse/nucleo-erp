"""Origen de las cuentas por pagar y resguardos contra editarlas o borrarlas.

La CxP nace cuando una factura de proveedor queda ``Registrada``. En cuanto tiene
un pago aplicado, ``PagoService`` ya descontó importes de su ``saldo`` y
``cancelar_pago`` recalcula su estatus contra ``total``: esos campos se congelan,
y la CxP deja de poder borrarse porque la cascada se llevaría las líneas del pago
y descuadraría el banco.
"""

from contextlib import contextmanager

from django.db import IntegrityError, OperationalError, transaction

from finanzas.exceptions import (
    LOCK_NOT_AVAILABLE,
    ConcurrentOperationError,
    ErrorDeNegocio,
    database_error_sqlstate,
)
from finanzas.models import CuentaPorPagar, FacturaProveedor, Pago, PagoDetalle

DUPLICATE_INVOICE_MESSAGE = "La factura de proveedor ya tiene una cuenta por pagar."

FROZEN_FIELD_LABELS = {
    "total": "el total",
    "saldo": "el saldo",
    "estatus": "el estatus",
    # Lo que ata la CxP a su factura: con pagos aplicados, moverla a otra factura
    # u otro proveedor arrastraría esos pagos a un documento ajeno.
    "factura_proveedor": "la factura de proveedor",
    "proveedor": "el proveedor",
}


class CuentaPorPagarService:
    @staticmethod
    def has_applied_payments(cxp_ids):
        return PagoDetalle.objects.filter(
            cxp_id__in=cxp_ids,
            pago__estatus=Pago.Estatus.APLICADO,
        ).exists()

    @staticmethod
    @contextmanager
    def duplicate_invoice_as_business_error(factura_proveedor_id, excluding_account_id=None):
        """Traduce la violación de ``uq_cxp_factura_proveedor`` a un 400.

        La validación del serializer cubre el caso normal; esto cubre la carrera
        de dos altas simultáneas para la misma factura. El punto de guardado deja
        utilizable la transacción envolvente para confirmar que es un duplicado.

        En una edición hay que excluir la propia CxP: si no, la comprobación
        siempre la encontraría a ella y cualquier IntegrityError ajeno se
        disfrazaría de factura duplicada.
        """
        try:
            with transaction.atomic():
                yield
        except IntegrityError:
            other_accounts = CuentaPorPagar.objects.filter(
                factura_proveedor_id=factura_proveedor_id
            )
            if excluding_account_id is not None:
                other_accounts = other_accounts.exclude(pk=excluding_account_id)
            if not other_accounts.exists():
                raise
            raise ErrorDeNegocio({"factura_proveedor": DUPLICATE_INVOICE_MESSAGE})

    @staticmethod
    @transaction.atomic
    def generate_for_invoice(factura):
        """Crea la CxP de una factura de proveedor que acaba de quedar ``Registrada``.

        Idempotente: si la factura ya tiene CxP (reentrada Registrada → Borrador →
        Registrada) no se crea otra ni se toca la existente, aunque el total de la
        factura haya cambiado entretanto.
        """
        if factura.estatus != FacturaProveedor.FacturaProveedorStatus.REGISTRADA:
            return None
        existing = CuentaPorPagar.objects.filter(factura_proveedor=factura).first()
        if existing is not None:
            return existing
        with CuentaPorPagarService.duplicate_invoice_as_business_error(factura.pk):
            return CuentaPorPagar.objects.create(
                empresa_id=factura.empresa_id,
                proveedor_id=factura.proveedor_id,
                factura_proveedor=factura,
                total=factura.total,
                saldo=factura.total,
                estatus=CuentaPorPagar.EstatusCxP.PENDIENTE,
                # Mismo criterio que la CxC que nace junto con su factura
                # (FacturaViewSet.registrar_pendiente_cobro): vence cuando vence
                # la factura.
                fecha_vencimiento=factura.fecha_vencimiento,
            )

    @staticmethod
    def ensure_frozen_fields_unchanged(cxp, validated_data):
        """Rechaza cambiar total, saldo, estatus, factura o proveedor de una CxP
        con pagos aplicados.

        Sólo cuenta como edición un valor distinto del vigente: un PUT que reenvía
        los valores actuales sigue pasando. ``fecha_vencimiento`` y
        ``observaciones`` quedan libres.
        """
        changed = [
            field
            for field in FROZEN_FIELD_LABELS
            if field in validated_data and validated_data[field] != getattr(cxp, field)
        ]
        if not changed or not CuentaPorPagarService.has_applied_payments([cxp.pk]):
            return
        raise ErrorDeNegocio(
            {
                field: (
                    f"No se puede modificar {FROZEN_FIELD_LABELS[field]} de una "
                    "cuenta por pagar con pagos aplicados."
                )
                for field in changed
            }
        )

    @staticmethod
    @transaction.atomic
    def ensure_account_deletable(cxp):
        CuentaPorPagarService._lock_and_ensure_without_applied_payments(
            [cxp.pk],
            "No se puede eliminar una cuenta por pagar con pagos aplicados. "
            "Cancele los pagos primero.",
        )

    @staticmethod
    @transaction.atomic
    def ensure_invoice_deletable(factura):
        cxp_ids = list(
            CuentaPorPagar.objects.filter(factura_proveedor=factura).values_list("pk", flat=True)
        )
        CuentaPorPagarService._lock_and_ensure_without_applied_payments(
            cxp_ids,
            "No se puede eliminar una factura de proveedor cuya cuenta por pagar "
            "tiene pagos aplicados. Cancele los pagos primero.",
        )

    @staticmethod
    def _lock_and_ensure_without_applied_payments(cxp_ids, message):
        if not cxp_ids:
            return
        # Bloqueos NOWAIT. PagoService bloquea en dos órdenes opuestos (aplicar_pago:
        # líneas → CxP en una misma sentencia; cancelar_pago y la cascada al borrar
        # el pago: CxP → líneas), así que ningún orden de espera en este guard evita
        # el deadlock con los dos. Sin esperar no se forma el ciclo: si otra
        # operación ya tiene las filas, el borrado desiste con un 409 y el cliente
        # reintenta. El punto de guardado deja la transacción utilizable tras el
        # 55P03; al liberarlo en el camino feliz, los bloqueos se conservan.
        try:
            with transaction.atomic():
                list(
                    PagoDetalle.objects.select_for_update(nowait=True)
                    .filter(cxp_id__in=cxp_ids)
                    .only("pk")
                )
                list(
                    CuentaPorPagar.objects.select_for_update(nowait=True)
                    .filter(pk__in=cxp_ids)
                    .only("pk")
                )
        except OperationalError as exc:
            if database_error_sqlstate(exc) != LOCK_NOT_AVAILABLE:
                raise
            raise ConcurrentOperationError(
                "No se puede eliminar en este momento: otra operación está modificando "
                "la cuenta por pagar o sus pagos. Intente de nuevo."
            ) from exc
        if CuentaPorPagarService.has_applied_payments(cxp_ids):
            raise ErrorDeNegocio(message)
