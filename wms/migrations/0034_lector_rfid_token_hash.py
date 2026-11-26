"""Lectores RFID (1/2): hash del token actual.

El token en claro de cada lector se reemplaza por su SHA-256 y sus últimos 4
caracteres: el FX sigue mandando el mismo token y sigue funcionando sin
reconfigurarlo. Ya no se puede recuperar el token desde el ERP (solo regenerar).
"""

import hashlib

from django.db import migrations, models


def token_a_hash(apps, schema_editor):
    LectorRFID = apps.get_model("wms", "LectorRFID")
    for lector in LectorRFID.objects.all().only("pk", "token"):
        LectorRFID.objects.filter(pk=lector.pk).update(
            token_hash=hashlib.sha256(lector.token.encode("utf-8")).hexdigest(),
            token_sufijo=lector.token[-4:],
        )



class Migration(migrations.Migration):

    dependencies = [
        ("wms", "0033_lector_rfid_en_uso"),
    ]

    operations = [
        # ── Token con hash ──────────────────────────────────────────────
        migrations.AddField(
            model_name="lectorrfid",
            name="token_hash",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="lectorrfid",
            name="token_sufijo",
            field=models.CharField(blank=True, default="", max_length=4),
        ),
        migrations.RunPython(token_a_hash, migrations.RunPython.noop),
    ]
