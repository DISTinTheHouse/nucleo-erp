from django.db import transaction
from django.core.exceptions import ValidationError
from nucleo.models import SerieFolio

@transaction.atomic
def generate_op_folio(empresa_id, sucursal_id):
    serie_folio = SerieFolio.objects.select_for_update().filter(
        empresa=empresa_id,
        sucursal=sucursal_id,
        tipo_documento__iexact='Orden de Produccion',
        activo=True
    ).order_by('id_serie_folio').first()

    if not serie_folio: raise ValidationError("No se encontro una serie de folio para la empresa y sucursal especificadas.")

    try:
        folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
    except Exception as e:
        raise ValidationError(f"Error al obtener el siguiente folio")
    
    serie_folio.folio_actual = nuevo_consecutivo
    serie_folio.ultimo_anio = anio_actual
    serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])

    return folio_formateado
    

    
        