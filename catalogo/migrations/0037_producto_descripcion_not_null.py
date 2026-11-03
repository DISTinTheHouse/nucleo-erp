from django.db import migrations


class Migration(migrations.Migration):
    """Alinea la BD con el modelo (``descripcion`` y ``updated_at`` sin ``null=True``). Solo BD:
    el estado de Django ya lo tiene como NOT NULL."""

    dependencies = [
        ("catalogo", "0036_producto_descripcion_sin_null"),
    ]

    operations = [
        migrations.RunSQL(
            "ALTER TABLE productos ALTER COLUMN descripcion SET NOT NULL, ALTER COLUMN updated_at SET NOT NULL",
            "ALTER TABLE productos ALTER COLUMN descripcion DROP NOT NULL, ALTER COLUMN updated_at DROP NOT NULL",
        ),
    ]
