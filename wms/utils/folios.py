from django.db import transaction
from rest_framework.exceptions import ValidationError
from nucleo.models import SerieFolio


@transaction.atomic
def generate_folio(empresa_id, sucursal_id, tipo_documento: str):
    """Shim backward-compatible. Delegación a ``SerieFolio.consumir_siguiente_folio``.

    Lanza ``rest_framework.exceptions.ValidationError`` (HTTP 400 DRF).
    """
    from django.core.exceptions import ValidationError as DjangoValidationError

    try:
        return SerieFolio.consumir_siguiente_folio(
            empresa_id,
            sucursal_id,
            [tipo_documento],
            descripcion_documento=tipo_documento,
        )
    except DjangoValidationError as e:
        msgs = getattr(e, "messages", [str(e)])
        msg = msgs[0] if msgs else str(e)
        raise ValidationError(msg)


@transaction.atomic
def generate_folio_multi_tipo(
    empresa_id,
    sucursal_id,
    tipos_documento,
    *,
    descripcion_documento=None,
):
    """Shim backward-compatible. Delegación a ``SerieFolio.consumir_siguiente_folio``.

    Lanza ``rest_framework.exceptions.ValidationError`` (HTTP 400 DRF).
    """
    from django.core.exceptions import ValidationError as DjangoValidationError

    try:
        return SerieFolio.consumir_siguiente_folio(
            empresa_id,
            sucursal_id,
            tipos_documento,
            descripcion_documento=descripcion_documento,
        )
    except DjangoValidationError as e:
        msgs = getattr(e, "messages", [str(e)])
        msg = msgs[0] if msgs else str(e)
        raise ValidationError(msg)
