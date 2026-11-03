from django.db import migrations
from django.db.models import F


def surtir_lo_asignado(apps, schema_editor):
    """Pickings previos: lo asignado cuenta como surtido, como en los nuevos."""
    Picking = apps.get_model("wms", "Picking")
    PickingDetalle = apps.get_model("wms", "PickingDetalle")
    cancelados = ("Cancelado", "CANCELADO")

    lineas = (
        PickingDetalle.objects.exclude(estado="CANCELADA")
        .exclude(picking__estado__in=cancelados)
        .filter(cantidad_surtida__lt=F("cantidad_asignada"))
    )
    lineas = list(lineas.select_related("picking"))
    for linea in lineas:
        linea.cantidad_surtida = linea.cantidad_asignada
        linea.estado = "SURTIDA"
        linea.fecha_surtido = linea.fecha_surtido or linea.picking.created_at
    PickingDetalle.objects.bulk_update(lineas, ["cantidad_surtida", "estado", "fecha_surtido"])

    # Mismo criterio que al crear: completado si cada línea cubrió lo pendiente de su talla.
    pickings = list(Picking.objects.filter(pk__in={linea.picking_id for linea in lineas}))
    for picking in pickings:
        if picking.total_lineas_completas >= picking.total_lineas:
            picking.estado = "Completado"
            picking.fecha_fin = picking.fecha_fin or picking.created_at
        else:
            picking.estado = "Parcial"
    Picking.objects.bulk_update(pickings, ["estado", "fecha_fin"])


class Migration(migrations.Migration):

    dependencies = [
        ("wms", "0018_picking_transferencia_variante_produccion"),
    ]

    operations = [
        migrations.RunPython(surtir_lo_asignado, migrations.RunPython.noop),
    ]
