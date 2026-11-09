"""#1024 (EC-833): da de baja los pedidos activos cuyo cliente es de otra empresa.

En producción sólo existe uno: el 256 (folio ``QA249-CONTAM``), un registro de
prueba de QA-249 sin renglones. Se da de baja (``activo=False``) en lugar de
moverlo de empresa: no es una venta real y la baja es reversible.
"""

from django.db import migrations
from django.db.models import F


def dar_de_baja(apps, schema_editor):
    Pedido = apps.get_model("ventas", "Pedido")
    Pedido.objects.filter(activo=True, cliente__isnull=False).exclude(
        cliente__empresa_id=F("empresa_id")
    ).update(activo=False)


class Migration(migrations.Migration):
    dependencies = [("ventas", "0060_aplanar_aprobado_snapshot")]

    operations = [migrations.RunPython(dar_de_baja, migrations.RunPython.noop)]
