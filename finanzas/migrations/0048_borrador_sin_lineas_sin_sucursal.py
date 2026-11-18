"""Cobros en Borrador sin líneas: sin sucursal (EC-874).

La sucursal de un cobro sale de las facturas de sus líneas; mientras no tiene líneas
es ``null`` y lo ve toda la empresa. Los Borradores vacíos creados antes (con la
``sucursal_default`` del usuario, #1063, o la primera de la empresa, 0047) la
conservaban y solo los veía esa sucursal. Solo toca esas filas: estatus
``Borrador`` (``Cobro.Estatus.BORRADOR``), sin ``cobro_detalles`` y con sucursal.
Repetirla no cambia nada. Reverso: no hace nada (la sucursal anterior no se guarda y
se recalcula al capturar líneas).
"""
from django.db import migrations
from django.db.models import Exists, OuterRef


def quitar_sucursal(apps, schema_editor):
    Cobro = apps.get_model("finanzas", "Cobro")
    CobroDetalle = apps.get_model("finanzas", "CobroDetalle")
    (
        Cobro.objects.filter(estatus="Borrador", sucursal__isnull=False)
        .exclude(Exists(CobroDetalle.objects.filter(cobro=OuterRef("pk"))))
        .update(sucursal=None)
    )


class Migration(migrations.Migration):

    dependencies = [
        ("finanzas", "0047_asignar_sucursal_cobro_pago"),
    ]

    operations = [migrations.RunPython(quitar_sucursal, migrations.RunPython.noop)]
