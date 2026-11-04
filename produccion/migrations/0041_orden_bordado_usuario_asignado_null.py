from django.db import migrations


class Migration(migrations.Migration):
    """Alinea la BD con el modelo (``usuario_asignado`` con ``null=True``). Solo BD: 0005 quedó
    marcada como aplicada con ``--fake`` (#872) y su ``DROP NOT NULL`` nunca corrió. En Postgres
    es idempotente: no falla si la columna ya admite nulos."""

    dependencies = [
        ("produccion", "0040_alter_ordenbordadodetalle_producto"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE orden_bordado ALTER COLUMN usuario_asignado_id DROP NOT NULL",
            "ALTER TABLE orden_bordado ALTER COLUMN usuario_asignado_id SET NOT NULL",
        ),
    ]
