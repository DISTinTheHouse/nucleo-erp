"""Uniforma las etiquetas de estatus ya registradas (#1065) con la misma regla
que usa la bitácora al escribir. Reverso: no hace nada."""
from django.db import migrations


def uniformar(apps, schema_editor):
    from auditoria.bitacora import etiqueta_uniforme

    Bitacora = apps.get_model("auditoria", "BitacoraDocumento")
    for fila in Bitacora.objects.only("pk", "estatus_anterior", "estatus_nuevo").iterator():
        ant, nue = etiqueta_uniforme(fila.estatus_anterior), etiqueta_uniforme(fila.estatus_nuevo)
        if ant != fila.estatus_anterior or nue != fila.estatus_nuevo:
            Bitacora.objects.filter(pk=fila.pk).update(estatus_anterior=ant[:40], estatus_nuevo=nue[:40])


class Migration(migrations.Migration):

    dependencies = [
        ("auditoria", "0006_bitacora_estatus_codigo"),
    ]

    operations = [migrations.RunPython(uniformar, migrations.RunPython.noop)]
