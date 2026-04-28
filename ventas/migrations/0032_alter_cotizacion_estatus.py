from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ventas", "0031_cotizacion_onboarding_sin_cliente"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cotizacion",
            name="estatus",
            field=models.SmallIntegerField(
                choices=[
                    (1, "BORRADOR"),
                    (2, "ENVIADA A AUTORIZACION"),
                    (3, "EN REVISION"),
                    (4, "RECHAZADA"),
                    (5, "CAMBIOS SOLICITADOS"),
                    (6, "AUTORIZADA"),
                    (7, "PENDIENTE POR ENVIAR A MESA DE CONTROL"),
                ],
                db_index=True,
                default=2,
            ),
        ),
    ]

