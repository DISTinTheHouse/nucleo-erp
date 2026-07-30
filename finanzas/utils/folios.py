from django.db import transaction
from django.core.exceptions import ValidationError
from nucleo.models import SerieFolio


@transaction.atomic
def generate_factura_folio(empresa_id, sucursal_id):
    """Shim backward-compatible. Delegación a ``SerieFolio.consumir_siguiente_folio``."""
    return SerieFolio.consumir_siguiente_folio(
        empresa_id,
        sucursal_id,
        ["Factura", "FACTURA", "Facturas", "FAC"],
        descripcion_documento="Factura",
    )
