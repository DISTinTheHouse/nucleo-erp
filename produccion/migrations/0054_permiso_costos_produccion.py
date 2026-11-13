"""Alta del permiso ``R-PRODUCCION-COSTOS`` (#1135): ver y capturar el costo de
maquila de la OP. El catálogo de permisos vive solo en la BD. Reverso: no hace nada."""
from django.db import migrations


def crear(apps, schema_editor):
    Permiso = apps.get_model("seguridad", "Permiso")
    Permiso.objects.get_or_create(
        clave="R-PRODUCCION-COSTOS",
        defaults={
            "nombre": "Ver costos de producción",
            "descripcion": "Ver y capturar el costo de maquila de las órdenes de producción",
            "modulo": "PRODUCCION",
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("seguridad", "0006_backfill_clave_departamento_roles"),
        ("produccion", "0053_serie_folio_salida_maquila"),
    ]

    operations = [migrations.RunPython(crear, migrations.RunPython.noop)]
