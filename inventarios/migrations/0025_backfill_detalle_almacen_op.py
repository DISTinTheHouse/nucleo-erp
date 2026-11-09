from datetime import timedelta

from django.db import migrations

VENTANA = timedelta(seconds=30)


def backfill_almacen_op(apps, schema_editor):
    """Renglones del consumo de OP sin ubicación ni almacén: el almacén sale del
    evento de auditoría de la misma OP y tipo (±30 s), como en 0021 (EC-821).

    Solo si hay un único candidato y es de la empresa del movimiento; lo ambiguo
    o sin evento se queda en null.
    """
    Detalle = apps.get_model("inventarios", "MovimientoInventarioDetalle")
    Almacen = apps.get_model("inventarios", "Almacen")
    AuditoriaEvento = apps.get_model("auditoria", "AuditoriaEvento")
    pendientes = Detalle.objects.filter(
        almacen__isnull=True, ubicacion_origen__isnull=True, ubicacion_destino__isnull=True,
        # Consumo (o recálculo) de OP; la entrada por recepción de OP tiene su almacén.
        movimiento_inventario__op__isnull=False,
        movimiento_inventario__recepcion__isnull=True,
    ).select_related("movimiento_inventario")
    for d in pendientes:
        m = d.movimiento_inventario
        if not m.fecha_movimiento:
            continue
        eventos = AuditoriaEvento.objects.filter(
            tabla="existencias", id_registro=str(m.op_id), accion=m.tipo_movimiento,
            created_at__gte=m.fecha_movimiento - VENTANA,
            created_at__lte=m.fecha_movimiento + VENTANA,
        )
        almacenes = {
            it.get("almacen_id")
            for ev in eventos
            for it in (ev.antes_json or {}).get("items", [])
            if it.get("producto_variante_id") == d.producto_variante_id
            and it.get("producto_id") == d.producto_id
            and it.get("ubicacion_id") is None
        }
        if len(almacenes) != 1 or None in almacenes:
            continue
        almacen_id = almacenes.pop()
        if Almacen.objects.filter(pk=almacen_id, empresa_id=m.empresa_id).exists():
            d.almacen_id = almacen_id
            d.save(update_fields=["almacen"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventarios", "0024_costeo_inventario_809"),
        ("auditoria", "0004_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_almacen_op, migrations.RunPython.noop),
    ]
