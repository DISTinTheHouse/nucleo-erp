from django.db import migrations

ANIO = "{anio}"


def a_marcador(apps, schema_editor):
    """#1142: el año capturado literal ("2026") en prefijo/sufijo pasa a ``{anio}``
    para que en 2027 los folios digan 2027. Mismo formato visible que hoy."""
    SerieFolio = apps.get_model("nucleo", "SerieFolio")
    for campo in ("prefijo", "sufijo"):
        SerieFolio.objects.filter(**{campo: "2026"}).update(**{campo: ANIO})


def a_literal(apps, schema_editor):
    SerieFolio = apps.get_model("nucleo", "SerieFolio")
    for campo in ("prefijo", "sufijo"):
        SerieFolio.objects.filter(**{campo: ANIO}).update(**{campo: "2026"})


class Migration(migrations.Migration):
    dependencies = [("nucleo", "0016_empresa_domicilio_fiscal")]
    operations = [migrations.RunPython(a_marcador, a_literal)]
