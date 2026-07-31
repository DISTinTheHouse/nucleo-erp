from django.db import transaction
from django.core.exceptions import ValidationError
from nucleo.models import SerieFolio


@transaction.atomic
def generate_op_folio(empresa_id, sucursal_id):
    """Shim backward-compatible. Delegación a ``SerieFolio.consumir_siguiente_folio``."""
    return SerieFolio.consumir_siguiente_folio(
        empresa_id,
        sucursal_id,
        ["Orden de Produccion", "ORDEN_PRODUCCION", "Orden Produccion", "OP"],
        descripcion_documento="Orden de Produccion",
    )


@transaction.atomic
def generate_ob_folio(empresa_id, sucursal_id):
    """Shim backward-compatible. Delegación a ``SerieFolio.consumir_siguiente_folio``."""
    return SerieFolio.consumir_siguiente_folio(
        empresa_id,
        sucursal_id,
        ["Orden de Bordado", "ORDEN_BORDADO", "Bordado", "OB"],
        descripcion_documento="Orden de Bordado",
    )


@transaction.atomic
def generate_or_folio(empresa_id, sucursal_id):
    return SerieFolio.consumir_siguiente_folio(
        empresa_id,
        sucursal_id,
        ["Orden de Reflejante", "ORDEN_REFLEJANTE", "Reflejante", "OR"],
        descripcion_documento="Orden de Reflejante",
    )

@transaction.atomic
def preview_ob_folio(empresa_id, sucursal_id):
    """Shim backward-compatible. Delegación a ``SerieFolio.preview_siguiente_folio``."""
    return SerieFolio.preview_siguiente_folio(
        empresa_id,
        sucursal_id,
        ["Orden de Bordado", "ORDEN_BORDADO", "Bordado", "OB"],
    )
