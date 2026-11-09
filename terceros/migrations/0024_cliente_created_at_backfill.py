"""Backfill de ``Cliente.created_at`` desde el historial (EC-817).

Cada cliente toma el ``history_date`` de su registro ``history_type='+'`` en
``terceros_historicalcliente``. Los que no tienen uno (anteriores a
django-simple-history, ids <= 16 en la base actual) quedan NULL: no se adivina.

Un solo ``UPDATE ... SET created_at = (subconsulta)``, sin recorrer clientes. El
``update()`` no dispara señales, así que no genera filas nuevas de historial.

Las filas existentes de ``HistoricalCliente`` NO se rellenan: son fotos de estados
pasados, nada lee ``created_at`` del historial, y el siguiente guardado de cada
cliente ya registra el valor.

Después del backfill, ``db_default=Now()``: en producción ``migrate_production`` corre
antes que ``deploy_production``, y el código viejo inserta clientes sin la columna;
sin default de BD esas altas quedarían NULL para siempre. Va DESPUÉS del backfill
porque en 0023 rellenaría las filas existentes con la hora de la migración; aquí es
solo ``ALTER COLUMN ... SET DEFAULT``, que no toca filas existentes.
django-simple-history copia el campo tal cual, así que el historial también lleva el
default (inofensivo: sus INSERT siempre traen el valor del cliente).

Reversa del backfill: no-op. Ponerlo en NULL borraría también el ``created_at`` real
de los clientes dados de alta después de la migración; si se revierte también 0023,
la columna desaparece de todos modos. La reversa de los ``AlterField`` quita el default.
"""

import django.db.models.functions.datetime
from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def rellenar_created_at(apps, schema_editor):
    Cliente = apps.get_model("terceros", "Cliente")
    HistoricalCliente = apps.get_model("terceros", "HistoricalCliente")
    alta = (
        HistoricalCliente.objects.filter(id=OuterRef("pk"), history_type="+")
        .order_by("history_date", "history_id")
        .values("history_date")[:1]
    )
    Cliente.objects.filter(created_at__isnull=True).update(created_at=Subquery(alta))


class Migration(migrations.Migration):

    dependencies = [
        ("terceros", "0023_cliente_created_at"),
    ]

    operations = [
        migrations.RunPython(rellenar_created_at, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="cliente",
            name="created_at",
            field=models.DateTimeField(
                auto_now_add=True, db_default=django.db.models.functions.datetime.Now(), null=True,
            ),
        ),
        migrations.AlterField(
            model_name="historicalcliente",
            name="created_at",
            field=models.DateTimeField(
                blank=True, db_default=django.db.models.functions.datetime.Now(), editable=False, null=True,
            ),
        ),
    ]
