from django.db import transaction
from rest_framework.exceptions import ValidationError
from nucleo.models import SerieFolio

@transaction.atomic
def generate_folio(empresa_id, sucursal_id, tipo_documento:str):
    serie_folio = SerieFolio.objects.select_for_update().filter(
        empresa=empresa_id,
        sucursal=sucursal_id,
        tipo_documento__iexact=tipo_documento,
        activo=True
    ).order_by('id_serie_folio').first()

    if not serie_folio: raise ValidationError("No se encontro una serie/folio para la empresa y sucursal especificadas.")

    try:
        folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
    except Exception as e:
        raise ValidationError(f"Error al obtener el siguiente folio")
    
    serie_folio.folio_actual = nuevo_consecutivo
    serie_folio.ultimo_anio = anio_actual
    serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])

    return folio_formateado


def _buscar_serie_folio(empresa_id, sucursal_id, tipos_candidatos):
    tipos_norm = [t for t in tipos_candidatos if t]
    for t in tipos_norm:
        sf = (
            SerieFolio.objects.select_for_update()
            .filter(
                empresa=empresa_id,
                sucursal=sucursal_id,
                tipo_documento__iexact=t,
                activo=True,
            )
            .order_by("id_serie_folio")
            .first()
        )
        if sf:
            return sf
    return None


@transaction.atomic
def safe_generate_folio(
    empresa_id,
    sucursal_id,
    tipos_documento,
    *,
    fallback_prefix="OT",
    fallback_reference=None,
):
    """Genera folio intentando múltiples tipos de documento; si ninguno existe
    construye un fallback silencioso basado en una referencia (ej: pedido.folio).

    No lanza ValidationError cuando falta la SerieFolio sembrada; sigue el
    mismo patrón que `ventas/api/views.py` con `OB-{pedido.folio}`.
    """
    serie_folio = _buscar_serie_folio(empresa_id, sucursal_id, tipos_documento)
    if serie_folio:
        try:
            folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
        except Exception:
            raise ValidationError("Error al obtener el siguiente folio")
        serie_folio.folio_actual = nuevo_consecutivo
        serie_folio.ultimo_anio = anio_actual
        serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])
        return folio_formateado

    ref = fallback_reference or ""
    sep = "-" if ref else ""
    return f"{fallback_prefix}{sep}{ref}"