from django.db import models


class TipoServicioBordado(models.TextChoices):
    """
    Catálogo central de TIPOS DE SERVICIO / TÉCNICAS que se le pueden aplicar
    a un bordado POR RENGLÓN (cada SKU/talla de un pedido).

    ✅ SSoT (Single Source of Truth) definido AQUÍ en Ventas: tanto Ventas
    al guardar el pedido cotización ONBOARDING como Producción al renderizar
    la Orden de Bordado usan este mismo diccionario.
    """

    NUEVO_PONCHADO = "NUEVO_PONCHADO", "Nuevo ponchado"
    SERIGRAFIA = "SERIGRAFIA", "Serigrafía"
    SUBLIMADO = "SUBLIMADO", "Sublimado"
    DTF = "DTF", "DTF"
    REVELADO = "REVELADO", "Revelado"


TIPO_SERVICIO_BORDADO_LABELS = dict(TipoServicioBordado.choices)
TIPO_SERVICIO_BORDADO_KEYS = {c[0] for c in TipoServicioBordado.choices}


def validar_tipos_servicio_array(value, campo_label="tipos_servicio"):
    """
    Validador reusable para el array `bordado_config.tipos_servicio` o
    cualquier otro campo que acepte los 5 servicios.

    Reglas:
      - Tipo list
      - Cada elemento es string y existe en TIPO_SERVICIO_BORDADO_KEYS
      - Sin duplicados

    Retorna lista validada (normalizada a keys estables) o lanza
    serializers.ValidationError / django.core.exceptions.ValidationError
    según el framework que la llame.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        from django.core.exceptions import ValidationError
        raise ValidationError(f"{campo_label} debe ser un arreglo de strings.")

    vistos = set()
    for item in value:
        if not isinstance(item, str):
            from django.core.exceptions import ValidationError
            raise ValidationError(f"Cada elemento de {campo_label} debe ser string.")
        if item not in TIPO_SERVICIO_BORDADO_KEYS:
            from django.core.exceptions import ValidationError
            permitidos = ", ".join(sorted(TIPO_SERVICIO_BORDADO_KEYS))
            raise ValidationError(
                f"{campo_label}={item!r} no permitido. Valores aceptados: {permitidos}."
            )
        if item in vistos:
            from django.core.exceptions import ValidationError
            raise ValidationError(f"{campo_label} tiene duplicados: {item!r} repetido.")
        vistos.add(item)
    return value


def tipos_servicio_display_list(keys):
    """
    Transforma `["NUEVO_PONCHADO", "SUBLIMADO"]` en labels listos para UI:
    `[{"value": "NUEVO_PONCHADO", "label": "Nuevo ponchado"}, {"value": ...}]`.
    """
    if not keys:
        return []
    return [
        {"value": k, "label": TIPO_SERVICIO_BORDADO_LABELS.get(k, k)}
        for k in keys
    ]
