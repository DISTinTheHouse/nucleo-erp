import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ventas", "0051_historicaloportunidad_remove_oportunidad_prospecto_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="oportunidad",
            name="empresa",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="oportunidades",
                to="nucleo.empresa",
            ),
        ),
    ]
