"""Backfill de ``Cliente.created_at`` desde el historial (EC-817).

Cada cliente toma el ``history_date`` de su registro ``history_type='+'`` en
``terceros_historicalcliente``. Los que no tienen uno (anteriores a
django-simple-history, ids <= 16 en la base actual) quedan NULL: no se adivina.

Un solo ``UPDATE ... SET created_at = (subconsulta)``, sin recorrer clientes. El
``update()`` no dispara señales, así que no genera filas nuevas de historial.

Las filas existentes de ``HistoricalCliente`` NO se rellenan: son fotos de estados
pasados, nada lee ``created_at`` del historial, y el siguiente guardado de cada
cliente ya registra el valor.

Reversa: no-op. Ponerlo en NULL borraría también el ``created_at`` real de los
clientes dados de alta después de la migración; si se revierte también 0023, la
columna desaparece de todos modos.
"""

from django.db import migrations
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
    ]
