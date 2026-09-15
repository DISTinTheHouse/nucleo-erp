"""Origen de las cuentas por pagar y resguardos contra editarlas o borrarlas.

La CxP nace cuando una factura de proveedor queda ``Registrada``. En cuanto tiene
un pago aplicado, ``PagoService`` ya descontó importes de su ``saldo`` y
``cancelar_pago`` recalcula su estatus contra ``total``: esos campos se congelan,
y la CxP deja de poder borrarse porque la cascada se llevaría las líneas del pago
y descuadraría el banco.
"""

from contextlib import contextmanager
from decimal import Decimal

from django.db import IntegrityError, OperationalError, transaction

from finanzas.exceptions import (
    LOCK_NOT_AVAILABLE,
    ConcurrentOperationError,
    ErrorDeNegocio,
    database_error_sqlstate,
)
from finanzas.models import CuentaPorPagar, FacturaProveedor, Pago, PagoDetalle

DUPLICATE_INVOICE_MESSAGE = "La factura de proveedor ya tiene una cuenta por pagar."
UNREGISTERED_INVOICE_MESSAGE = (
    "Sólo una factura de proveedor Registrada puede tener cuenta por pagar."
)
UNREGISTERED_INVOICE_REVIVAL_MESSAGE = (
    "Sólo se puede reactivar la cuenta por pagar de una factura de proveedor Registrada."
)

# Lo que la CxP toma de su factura: ``total`` y ``proveedor`` los copió al nacer y
# la generación es idempotente, así que no vuelve a sincronizarlos; ``moneda`` la
# lee a través de la FK, y cambiarla movería la moneda de la CxP sin tocar sus
# importes.
INVOICE_ACCOUNT_FIELD_LABELS = {
    "total": "el total",
    "proveedor": "el proveedor",
    "moneda": "la moneda",
}
# Estatus de factura que no pueden respaldar una CxP viva.
INVOICE_STATUSES_WITHOUT_ACCOUNT = frozenset({
    FacturaProveedor.FacturaProveedorStatus.BORRADOR,
    FacturaProveedor.FacturaProveedorStatus.CANCELADA,
})

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

        Idempotente: si la factura ya tiene CxP viva (Pendiente/Parcial/Pagada) no
        se crea otra ni se toca la existente, aunque el total de la factura haya
        cambiado entretanto (reentrada Registrada → Borrador → Registrada).

        Si la CxP existente está ``Cancelada`` --y por tanto ya no retiene a la
        factura (ver ``ensure_invoice_edit_keeps_account``)-- esta reentrada la
        revive en la misma fila en vez de intentar crear una segunda, que chocaría
        con ``uq_cxp_factura_proveedor``. Revivir resetea sólo lo que una
        generación fresca fija: total, saldo, proveedor, vencimiento, estatus y
        último pago. ``fecha_emision``, ``observaciones`` y ``empresa`` no se
        tocan, y los pagos en Borrador que sigan apuntando a la CxP quedan igual:
        los administra el operador.
        """
        if factura.estatus != FacturaProveedor.FacturaProveedorStatus.REGISTRADA:
            return None
        existing = (
            CuentaPorPagar.objects.select_for_update()
            .filter(factura_proveedor=factura)
            .first()
        )
        if existing is not None:
            if existing.estatus != CuentaPorPagar.EstatusCxP.CANCELADA:
                return existing
            if CuentaPorPagarService.has_applied_payments([existing.pk]):
                raise ErrorDeNegocio(
                    {
                        "factura_proveedor": (
                            "La cuenta por pagar cancelada de esta factura tiene "
                            "pagos aplicados: no se puede revivir."
                        )
                    }
                )
            existing.total = factura.total
            existing.saldo = factura.total
            existing.proveedor_id = factura.proveedor_id
            existing.fecha_vencimiento = factura.fecha_vencimiento
            existing.estatus = CuentaPorPagar.EstatusCxP.PENDIENTE
            existing.fecha_ultimo_pago = None
            existing.save()
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
    def ensure_invoice_edit_keeps_account(factura, validated_data):
        """Rechaza cambiar total, proveedor o moneda de una factura de proveedor con
        CxP, o regresarla a Borrador o Cancelada.

        ``factura`` debe ser la fila bloqueada y releída: toda vía que cuelga una
        CxP de una factura (su registro, el alta manual y el re-apuntado de una CxP)
        bloquea antes la factura, así que tras el lock la existencia de la CxP es
        confiable. Igual que ``ensure_frozen_fields_unchanged``, sólo cuenta un valor
        distinto del vigente, y la CxP se consulta sólo si algo cambia.
        """
        changed = [
            field
            for field in INVOICE_ACCOUNT_FIELD_LABELS
            if field in validated_data and validated_data[field] != getattr(factura, field)
        ]
        new_status = validated_data.get("estatus", factura.estatus)
        moves_to_status_without_account = (
            new_status != factura.estatus and new_status in INVOICE_STATUSES_WITHOUT_ACCOUNT
        )
        if not changed and not moves_to_status_without_account:
            return
        # Una CxP cancelada ya no respalda nada: no retiene a su factura. Sólo las
        # vivas (Pendiente, Parcial, Pagada) la congelan.
        live_accounts = CuentaPorPagar.objects.filter(factura_proveedor=factura).exclude(
            estatus=CuentaPorPagar.EstatusCxP.CANCELADA
        )
        if not live_accounts.exists():
            return
        errors = {
            field: (
                f"No se puede modificar {INVOICE_ACCOUNT_FIELD_LABELS[field]} de una "
                "factura de proveedor que ya tiene cuenta por pagar."
            )
            for field in changed
        }
        if moves_to_status_without_account:
            errors["estatus"] = (
                f"No se puede pasar a {new_status} una factura de proveedor que ya "
                "tiene cuenta por pagar."
            )
        raise ErrorDeNegocio(errors)

    @staticmethod
    def ensure_account_matches_invoice(factura, proveedor=None, total=None, moneda_id=None):
        """Cruza contra la factura lo que la CxP toma de ella.

        Se usa dos veces, igual que ``ensure_invoice_registered``: antes del lock
        desde el serializer (DRF traduce el ``ErrorDeNegocio`` a 400 por campo) y
        otra vez con la factura bloqueada. Hasta que la CxP cuelga de ella nada la
        congela, así que entre la validación y el lock la factura pudo cambiar de
        total, de proveedor o de moneda, y el descuadre resultante ya sería
        permanente: con CxP, ambos lados quedan congelados.

        ``moneda_id`` es la moneda vigente de la CxP --la de su factura actual-- y
        sólo se cruza al re-apuntarla: la CxP no guarda moneda propia, la lee de su
        factura, así que moverla a una factura de otra moneda la redenominaría
        dejando los importes intactos. Cada desajuste se reporta en su campo.
        """
        errors = {}
        if proveedor is not None and proveedor.pk != factura.proveedor_id:
            errors["proveedor"] = "El proveedor no coincide con el de la factura de proveedor."
        if total is not None:
            # ``total`` es opcional en el modelo: si no llega vale 0 y así se compara.
            submitted = Decimal(str(total or 0)).quantize(Decimal("0.01"))
            expected = Decimal(str(factura.total or 0)).quantize(Decimal("0.01"))
            if submitted != expected:
                errors["total"] = (
                    f"El total ({submitted}) no coincide con el de la factura "
                    f"de proveedor ({expected})."
                )
        if moneda_id is not None and moneda_id != factura.moneda_id:
            errors["moneda"] = "La moneda no coincide con la de la factura de proveedor."
        if errors:
            raise ErrorDeNegocio(errors)

    @staticmethod
    def ensure_invoice_registered(factura):
        """Rechaza colgar una CxP de una factura que no está ``Registrada``.

        Vale para el alta manual y para re-apuntar una CxP existente a otra factura.
        """
        if factura.estatus != FacturaProveedor.FacturaProveedorStatus.REGISTRADA:
            raise ErrorDeNegocio({"factura_proveedor": UNREGISTERED_INVOICE_MESSAGE})

    @staticmethod
    def ensure_account_revived_only_for_registered_invoice(cxp, factura, validated_data):
        """Rechaza sacar de ``Cancelada`` una CxP cuya factura no está ``Registrada``.

        Misma regla que el alta y el re-apuntado: sólo una factura Registrada
        respalda una CxP viva. Con la factura Cancelada quedaría respaldando un
        documento dado de baja; en Borrador, uno que todavía no se registra.
        ``factura`` es la que respaldará a la CxP tras guardar, ya bloqueada. La
        revivificación de ``generate_for_invoice`` no pasa por aquí: la dispara
        re-registrar la factura, y para entonces ya está Registrada.
        """
        new_status = validated_data.get("estatus", cxp.estatus)
        if CuentaPorPagarService._revives_without_registered_invoice(cxp, factura, new_status):
            raise ErrorDeNegocio({"estatus": UNREGISTERED_INVOICE_REVIVAL_MESSAGE})

    @staticmethod
    def ensure_payment_revives_accounts_only_for_registered_invoices(cxps, facturas):
        """La misma regla cuando la CxP revive por aplicarle un pago.

        ``aplicar_pago`` deja cada CxP en Parcial o Pagada sin mirar su estatus: un
        pago en Borrador que sigue apuntando a una CxP cancelada, o uno nuevo, la
        revivía aunque su factura estuviera Cancelada o en Borrador. ``cxps`` y
        ``facturas`` son las filas bloqueadas, indexadas por pk.
        """
        for cxp in cxps.values():
            factura = facturas[cxp.factura_proveedor_id]
            # Aplicar siempre deja la CxP en un estatus vivo.
            if CuentaPorPagarService._revives_without_registered_invoice(
                cxp, factura, CuentaPorPagar.EstatusCxP.PARCIAL
            ):
                raise ErrorDeNegocio(
                    {
                        "pago_detalles": (
                            f"No se puede aplicar un pago a la CxP {cxp.pk}: está "
                            "cancelada y sólo se puede reactivar la cuenta por pagar "
                            "de una factura de proveedor Registrada."
                        )
                    }
                )

    @staticmethod
    def _revives_without_registered_invoice(cxp, factura, new_status):
        return (
            cxp.estatus == CuentaPorPagar.EstatusCxP.CANCELADA
            and new_status != CuentaPorPagar.EstatusCxP.CANCELADA
            and factura.estatus != FacturaProveedor.FacturaProveedorStatus.REGISTRADA
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
