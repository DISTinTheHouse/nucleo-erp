import re

from django.db import migrations

PATRON = re.compile(r"^Despacho (\d+) del pedido ")


def ligar_salidas_de_despacho(apps, schema_editor):
    """#1250: las salidas que ya creó el despacho (``DespachoService._registrar_salida``)
    se identificaban solo por ``observaciones`` ("Despacho {id} del pedido …"). Se
    ligan a su despacho para que no cuenten como salida manual del pedido."""
    MovimientoInventario = apps.get_model("inventarios", "MovimientoInventario")
    Despacho = apps.get_model("wms", "Despacho")
    candidatos = MovimientoInventario.objects.filter(
        tipo_movimiento="SALIDA", despacho__isnull=True, pedido__isnull=False,
        observaciones__startswith="Despacho ",
    ).only("id", "observaciones", "pedido_id")
    for mov in candidatos:
        m = PATRON.match(mov.observaciones or "")
        if not m:
            continue
        despacho = Despacho.objects.filter(pk=int(m.group(1)), packing__pedido_id=mov.pedido_id).only("id").first()
        if despacho is not None:
            MovimientoInventario.objects.filter(pk=mov.pk).update(despacho_id=despacho.pk)


class Migration(migrations.Migration):
    # Aparte del esquema: en Postgres, actualizar la FK y crear su índice en la
    # misma transacción falla ("pending trigger events").
    dependencies = [("inventarios", "0027_movimiento_despacho")]
    operations = [migrations.RunPython(ligar_salidas_de_despacho, migrations.RunPython.noop)]
