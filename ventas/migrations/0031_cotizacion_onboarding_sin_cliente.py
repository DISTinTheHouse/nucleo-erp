from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("ventas", "0030_alter_cotizacion_estatus"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cotizacion",
            name="cliente",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="cotizacion",
                to="terceros.cliente",
            ),
        ),
        migrations.AlterField(
            model_name="cotizacion",
            name="persona_pagos",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AlterField(
            model_name="cotizacion",
            name="correo_facturas",
            field=models.EmailField(blank=True, default="", max_length=150),
        ),
        migrations.AlterField(
            model_name="cotizacion",
            name="telefono_pagos",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
    ]

