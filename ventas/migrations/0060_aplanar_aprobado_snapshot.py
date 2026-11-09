"""EC-801: quita el ``aprobado_snapshot`` anidado dentro de cada snapshot.

Solo se conserva el último aprobado; ``rechazar-cambios`` nunca usó los niveles
anidados. Irreversible a propósito: el anidado no tiene uso.
"""
from django.db import migrations


def aplanar(apps, schema_editor):
    Cotizacion = apps.get_model("ventas", "Cotizacion")
    cambiadas = []
    for cot in Cotizacion.objects.filter(aprobado_snapshot__cotizacion__has_key="aprobado_snapshot").only("id", "aprobado_snapshot"):
        snap = cot.aprobado_snapshot
        snap["cotizacion"].pop("aprobado_snapshot", None)
        cambiadas.append(cot)
    Cotizacion.objects.bulk_update(cambiadas, ["aprobado_snapshot"], batch_size=100)


class Migration(migrations.Migration):
    dependencies = [("ventas", "0059_servicio_extra_factura")]

    operations = [migrations.RunPython(aplanar, migrations.RunPython.noop)]
