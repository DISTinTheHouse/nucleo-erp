from collections import defaultdict
from datetime import timedelta

from django.db import migrations

VENTANA = timedelta(seconds=30)


def backfill_almacen_op(apps, schema_editor):
    """Renglones del consumo de insumos de OP sin ubicación ni almacén: el
    almacén sale del evento de auditoría de esa misma OP y tipo (±30 s), como en
    0021 (EC-821).

    Solo si hay un único candidato y es de la empresa y sucursal del movimiento;
    lo ambiguo o sin evento se queda en null. Los renglones del recálculo de
    tela no se recuperan: su evento (``consumos_produccion``) no trae almacén.
    """
    Detalle = apps.get_model("inventarios", "MovimientoInventarioDetalle")
    Almacen = apps.get_model("inventarios", "Almacen")
    AuditoriaEvento = apps.get_model("auditoria", "AuditoriaEvento")
    pendientes = [
        d for d in Detalle.objects.filter(
            almacen__isnull=True, ubicacion_origen__isnull=True, ubicacion_destino__isnull=True,
            movimiento_inventario__op__isnull=False,
            # La entrada por recepción de OP toma su almacén de la recepción.
            movimiento_inventario__recepcion__isnull=True,
            movimiento_inventario__fecha_movimiento__isnull=False,
        ).select_related("movimiento_inventario")
    ]
    if not pendientes:
        return

    # Una consulta: los eventos de existencias de las OP pendientes en la
    # ventana total. ``id_registro`` es compartido con pedidos y almacenes, así
    # que la OP se confirma con ``antes_json.op_id`` (lo escribe el consumo).
    movs = [d.movimiento_inventario for d in pendientes]
    eventos_por_op = defaultdict(list)
    for ev in AuditoriaEvento.objects.filter(
        tabla="existencias",
        id_registro__in={str(m.op_id) for m in movs},
        accion__in={m.tipo_movimiento for m in movs},
        created_at__gte=min(m.fecha_movimiento for m in movs) - VENTANA,
        created_at__lte=max(m.fecha_movimiento for m in movs) + VENTANA,
    ).only("id_registro", "accion", "created_at", "antes_json"):
        payload = ev.antes_json if isinstance(ev.antes_json, dict) else {}
        if str(payload.get("op_id")) != ev.id_registro:
            continue
        eventos_por_op[(ev.id_registro, ev.accion)].append((ev.created_at, payload.get("items") or []))

    candidatos = {}
    for d in pendientes:
        m = d.movimiento_inventario
        almacenes = {
            it.get("almacen_id")
            for creado, items in eventos_por_op.get((str(m.op_id), m.tipo_movimiento), [])
            if abs(creado - m.fecha_movimiento) <= VENTANA
            for it in items
            if isinstance(it, dict)
            and it.get("producto_variante_id") == d.producto_variante_id
            and it.get("producto_id") == d.producto_id
            and it.get("ubicacion_id") is None
        }
        if len(almacenes) == 1 and None not in almacenes:
            candidatos[d] = almacenes.pop()

    # Una consulta: empresa y sucursal de los almacenes candidatos.
    alcance = {
        pk: (empresa_id, sucursal_id)
        for pk, empresa_id, sucursal_id in Almacen.objects.filter(pk__in=set(candidatos.values()))
        .values_list("pk", "empresa_id", "sucursal_id")
    }
    actualizar = []
    for d, almacen_id in candidatos.items():
        m = d.movimiento_inventario
        if alcance.get(almacen_id) == (m.empresa_id, m.sucursal_id):
            d.almacen_id = almacen_id
            actualizar.append(d)
    Detalle.objects.bulk_update(actualizar, ["almacen"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventarios", "0024_costeo_inventario_809"),
        ("auditoria", "0004_initial"),
    ]

    operations = [
        migrations.RunPython(backfill_almacen_op, migrations.RunPython.noop),
    ]
