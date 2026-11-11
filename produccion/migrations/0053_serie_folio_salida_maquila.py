"""Crea la serie de folio ``SM`` (Salida a Maquila) en cada empresa/sucursal que ya
tiene serie de OP, con su mismo formato y el consecutivo en cero. Reverso: no hace nada."""
from django.db import migrations

TIPOS_OP = ["Orden de Produccion", "ORDEN_PRODUCCION", "Orden Produccion", "OP"]


def crear(apps, schema_editor):
    SerieFolio = apps.get_model("nucleo", "SerieFolio")
    for op in SerieFolio.objects.filter(tipo_documento__in=TIPOS_OP, activo=True):
        if SerieFolio.objects.filter(
            empresa_id=op.empresa_id, sucursal_id=op.sucursal_id, tipo_documento="Salida a Maquila"
        ).exists():
            continue
        SerieFolio.objects.create(
            empresa_id=op.empresa_id, sucursal_id=op.sucursal_id,
            tipo_documento="Salida a Maquila", serie="SM", folio_actual=0,
            relleno_ceros=op.relleno_ceros, separador=op.separador,
            incluir_anio=op.incluir_anio, reiniciar_anual=op.reiniciar_anual, activo=True,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0016_empresa_domicilio_fiscal"),
        ("produccion", "0052_salida_maquila"),
    ]

    operations = [migrations.RunPython(crear, migrations.RunPython.noop)]
