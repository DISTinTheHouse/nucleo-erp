from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('terceros', '0014_historicalcliente_historicaldireccioncliente_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='historicalproveedor',
            name='fax',
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
        migrations.AlterField(
            model_name='proveedor',
            name='fax',
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
    ]
