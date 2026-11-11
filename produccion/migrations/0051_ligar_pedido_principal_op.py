"""Cada OP con pedido queda ligada a él en ``orden_produccion_pedidos``.
Reverso: no hace nada (la tabla se borra con la migración anterior)."""
from django.db import migrations


def ligar(apps, schema_editor):
    OP = apps.get_model("produccion", "OrdenProduccion")
    OPP = apps.get_model("produccion", "OrdenProduccionPedido")
    existentes = set(OPP.objects.values_list("op_id", "pedido_id"))
    OPP.objects.bulk_create([
        OPP(op_id=op_id, pedido_id=pedido_id)
        for op_id, pedido_id in OP.objects.filter(pedido__isnull=False).values_list("op_id", "pedido_id")
        if (op_id, pedido_id) not in existentes
    ])


class Migration(migrations.Migration):

    dependencies = [
        ("produccion", "0050_orden_produccion_pedidos"),
    ]

    operations = [migrations.RunPython(ligar, migrations.RunPython.noop)]
