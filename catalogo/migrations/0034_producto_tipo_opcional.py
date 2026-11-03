import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0033_categoria_linea_asignar'),
    ]

    # Sólo ``DROP NOT NULL`` (inverso de 0031): el ``AlterField`` de Django tira y
    # recrea la FK, que bloquea también ``tipo_producto``.
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "ALTER TABLE productos ALTER COLUMN tipo_id DROP NOT NULL",
                    "ALTER TABLE productos ALTER COLUMN tipo_id SET NOT NULL",
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name='producto',
                    name='tipo',
                    field=models.ForeignKey(blank=True, limit_choices_to={'activo': True}, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='productos', to='catalogo.tipoproducto'),
                ),
            ],
        ),
    ]
