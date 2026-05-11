from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ventas', '0033_cotizaciondetalletalla_sku'),
    ]

    operations = [
        migrations.AddField(
            model_name='cotizacionservicioextra',
            name='cantidad',
            field=models.PositiveIntegerField(default=1, verbose_name='cantidad'),
        ),
    ]
