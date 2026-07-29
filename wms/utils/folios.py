from django.db import transaction
from rest_framework.exceptions import ValidationError
from nucleo.models import SerieFolio


def _consumir_folio(serie_folio):
    """Obtiene el siguiente folio de una ``SerieFolio`` ya resuelta y lo persiste.

    Único punto de consumo compartido por ``generate_folio`` y
    ``generate_folio_multi_tipo``: ambas difieren únicamente en *cómo* resuelven
    la ``SerieFolio`` (un ``tipo_documento`` exacto vs. varios candidatos), no en
    qué hacen con ella una vez encontrada.

    La única excepción esperada de ``get_siguiente_folio()`` es ``ValueError``
    ("Rango de folios agotado"), la validación de negocio de ``folio_final``. Se
    traduce a ``ValidationError``; cualquier otra excepción indicaría un bug
    (un campo mal configurado, por ejemplo) y debe propagarse tal cual, no
    disfrazarse del mismo mensaje genérico de "error al obtener el folio".
    """
    try:
        folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
    except ValueError:
        raise ValidationError("Error al obtener el siguiente folio")

    serie_folio.folio_actual = nuevo_consecutivo
    serie_folio.ultimo_anio = anio_actual
    serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])
    return folio_formateado


@transaction.atomic
def generate_folio(empresa_id, sucursal_id, tipo_documento: str):
    serie_folio = (
        SerieFolio.objects.select_for_update()
        .filter(
            empresa=empresa_id,
            sucursal=sucursal_id,
            tipo_documento__iexact=tipo_documento,
            activo=True,
        )
        .order_by("id_serie_folio")
        .first()
    )
    if not serie_folio:
        raise ValidationError("No se encontro una serie/folio para la empresa y sucursal especificadas.")
    return _consumir_folio(serie_folio)


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
def generate_folio_multi_tipo(
    empresa_id,
    sucursal_id,
    tipos_documento,
    *,
    descripcion_documento=None,
):
    """Genera un folio probando varios ``tipo_documento`` equivalentes.

    Existe porque un mismo documento puede estar sembrado con nombres distintos
    según la empresa (``ORDEN_BORDADO``, ``Orden de Bordado``, ``Bordado``): se
    prueban en orden y gana la primera serie que exista.

    Si no hay ninguna serie sembrada **se rechaza con ``ValidationError``**, igual
    que ``generate_folio``. Antes se devolvía un folio inventado a partir del
    pedido (``OB-{pedido.folio}``), pero ese valor es constante por pedido y los
    campos destino son ``unique=True``: en cuanto un mismo pedido generaba una
    segunda orden del mismo tipo —posible desde que el picking es parcial y
    repetible— el segundo insert reventaba con ``IntegrityError`` (500). Un folio
    que no sale de una serie tampoco es un identificador de documento válido:
    falta de configuración es un error que hay que mostrar, no tapar.

    ``descripcion_documento`` es el nombre legible que aparece en el error.
    """
    serie_folio = _buscar_serie_folio(empresa_id, sucursal_id, tipos_documento)
    if not serie_folio:
        documento = descripcion_documento or " / ".join(
            str(tipo) for tipo in tipos_documento if tipo
        )
        raise ValidationError(
            f"No se encontró una serie de folio activa para {documento} en la empresa "
            "y sucursal indicadas. Configure la serie antes de generar el documento."
        )

    return _consumir_folio(serie_folio)