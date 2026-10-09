from django.db import transaction
from django.core.exceptions import ValidationError
from nucleo.models import SerieFolio


TIPOS_DOCUMENTO_FACTURA = ["Factura", "FACTURA", "Facturas", "FAC"]
TIPOS_DOCUMENTO_POLIZA = ["Poliza", "POLIZA", "Polizas", "POL"]


@transaction.atomic
def generate_factura_folio(empresa_id, sucursal_id):
    """Shim backward-compatible. Delegación a ``SerieFolio.consumir_siguiente_folio``."""
    return SerieFolio.consumir_siguiente_folio(
        empresa_id,
        sucursal_id,
        TIPOS_DOCUMENTO_FACTURA,
        descripcion_documento="Factura",
    )


@transaction.atomic
def generate_poliza_folio(empresa_id, sucursal_id):
    """``(folio, consecutivo)`` de la póliza, consumido de forma atómica.

    Sustituye al ``MAX(folio_consecutivo) + 1`` que se leía sin ``select_for_update``
    ni unicidad: dos peticiones concurrentes producían el mismo ``POL-000001``.

    A diferencia de la factura, si no hay serie activa **se crea**. El folio de
    póliza es interno, no fiscal: que la contabilización falle porque nadie
    sembró el catálogo sería peor que estrenar la serie aquí. Es el único lugar
    del proyecto que auto-provisiona una ``SerieFolio``.
    """
    # Los call sites pasan indistintamente la instancia o el id, igual que a
    # ``generate_factura_folio``: ``resolve`` filtra con ambos, pero crear la
    # serie necesita las llaves.
    empresa_pk = getattr(empresa_id, "pk", empresa_id)
    sucursal_pk = getattr(sucursal_id, "pk", sucursal_id)

    if SerieFolio.resolve(empresa_id, sucursal_id, TIPOS_DOCUMENTO_POLIZA, lock=True) is None:
        # ``get_or_create`` y no ``create``: dos contabilizaciones simultáneas de
        # una empresa estrenando su serie chocarían contra
        # ``uq_serie_folio_sucursal_tipo_serie``. Reabre la serie si estaba
        # desactivada, porque ``resolve`` sólo ve las activas y si no el folio
        # quedaría inalcanzable con la fila ya ocupando la combinación única.
        serie, creada = SerieFolio.objects.get_or_create(
            sucursal_id=sucursal_pk,
            tipo_documento="Poliza",
            serie="POL",
            defaults={
                "empresa_id": empresa_pk,
                "folio_actual": 0,
                "relleno_ceros": 6,
            },
        )
        if not creada and not serie.activo:
            serie.activo = True
            serie.save(update_fields=["activo", "updated_at"])

    return SerieFolio.consumir_siguiente_folio_detallado(
        empresa_id,
        sucursal_id,
        TIPOS_DOCUMENTO_POLIZA,
        descripcion_documento="Póliza",
    )
