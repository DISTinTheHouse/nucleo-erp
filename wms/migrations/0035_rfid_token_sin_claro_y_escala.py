"""Lectores RFID (2/2): quita el token en claro, rate limit, índices y última lectura por EPC."""

import django.db.models.deletion
from django.db import migrations, models


def llenar_ultimas(apps, schema_editor):
    """Una fila por (lector, epc) con su lectura más reciente."""
    schema_editor.execute(
        """
        INSERT INTO rfid_lecturas_ultimas (empresa_id, lector_id, epc, scan_id, visto)
        SELECT DISTINCT ON (s.lector_id, s.epc)
               s.empresa_id, s.lector_id, s.epc, s.id, s.created_at
          FROM rfid_scans s
         WHERE s.lector_id IS NOT NULL AND s.empresa_id IS NOT NULL
         ORDER BY s.lector_id, s.epc, s.created_at DESC, s.id DESC
        """
    )


class Migration(migrations.Migration):

    dependencies = [
        ("nucleo", "0017_serie_folio_anio_calculado"),
        ("wms", "0034_lector_rfid_token_hash"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lectorrfid",
            name="token_hash",
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.RemoveField(model_name="lectorrfid", name="token"),
        # ── Rate limit por token ────────────────────────────────────────
        migrations.AddField(
            model_name="lectorrfid",
            name="rl_ventana",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lectorrfid",
            name="rl_conteo",
            field=models.PositiveIntegerField(default=0),
        ),
        # ── Índices de lecturas ─────────────────────────────────────────
        migrations.AddIndex(
            model_name="rfidscan",
            index=models.Index(fields=["empresa", "-created_at", "-id"], name="rfid_scan_emp_fecha_idx"),
        ),
        migrations.AddIndex(
            model_name="rfidscan",
            index=models.Index(fields=["lector", "-created_at", "-id"], name="rfid_scan_lector_fecha_idx"),
        ),
        migrations.AddIndex(
            model_name="rfidscan",
            index=models.Index(fields=["created_at"], name="rfid_scan_fecha_idx"),
        ),
        # ── Última lectura por (lector, epc) ────────────────────────────
        migrations.CreateModel(
            name="RfidLecturaUltima",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("epc", models.CharField(max_length=255)),
                ("visto", models.DateTimeField()),
                ("empresa", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="+", to="nucleo.empresa")),
                ("lector", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ultimas_lecturas", to="wms.lectorrfid")),
                ("scan", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="es_ultima", to="wms.rfidscan")),
            ],
            options={
                "verbose_name": "Última lectura RFID",
                "verbose_name_plural": "Últimas lecturas RFID",
                "db_table": "rfid_lecturas_ultimas",
                "constraints": [models.UniqueConstraint(fields=("lector", "epc"), name="uq_rfid_ultima_lector_epc")],
                "indexes": [models.Index(fields=["empresa", "epc", "-visto"], name="rfid_ultima_emp_epc_idx")],
            },
        ),
        migrations.RunPython(llenar_ultimas, migrations.RunPython.noop),
    ]
