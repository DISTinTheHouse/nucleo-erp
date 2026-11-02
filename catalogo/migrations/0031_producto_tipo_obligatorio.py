import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0030_tipo_producto_backfill'),
    ]

    # Sólo ``SET NOT NULL``: el ``AlterField`` de Django tira y recrea la FK, que
    # bloquea también ``tipo_producto`` y chocó con tráfico vivo (deadlock).
    # ``PROTECT`` y ``limit_choices_to`` no existen en la BD.
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "ALTER TABLE productos ALTER COLUMN tipo_id SET NOT NULL",
                    "ALTER TABLE productos ALTER COLUMN tipo_id DROP NOT NULL",
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name='producto',
                    name='tipo',
                    field=models.ForeignKey(limit_choices_to={'activo': True}, on_delete=django.db.models.deletion.PROTECT, related_name='productos', to='catalogo.tipoproducto'),
                ),
            ],
        ),
    ]
