"""``Transferencia.picking`` (EC-916): el picking que originó cada TR.

``Picking.transferencia`` solo guarda la última TR (``trasladar()`` la pisa) y la
vista 360 del pedido encontraba las demás con un regex sobre ``observaciones``,
texto que cualquiera puede escribir por la API. Esta FK la fija el servidor.

Backfill: ``wms.services.transferencia_picking_service.ligar`` (reglas en su
docstring; solo liga lo que no deja duda). Es idempotente y el pipeline lo vuelve
a correr tras cada despliegue (``manage.py ligar_transferencias_picking``) para
las TR que cree el build anterior mientras se despliega.
"""

import django.db.models.deletion
from django.db import migrations, models


def ligar_transferencias(apps, schema_editor):
    from wms.services.transferencia_picking_service import ligar

    ligar(
        apps.get_model("wms", "Transferencia"),
        apps.get_model("wms", "Picking"),
        apps.get_model("auditoria", "AuditoriaEvento"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('auditoria', '0007_bitacora_etiquetas_uniformes'),
        ('wms', '0020_rfid_lectura_ligada_a_etiqueta'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicaltransferencia',
            name='picking',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='wms.picking'),
        ),
        migrations.AddField(
            model_name='transferencia',
            name='picking',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transferencias', to='wms.picking'),
        ),
        # Al revertir, el RemoveField de la columna deshace el backfill.
        migrations.RunPython(ligar_transferencias, migrations.RunPython.noop),
    ]
