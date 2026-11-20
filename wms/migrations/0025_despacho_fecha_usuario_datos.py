from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("inventarios", "0029_movimiento_detalle_antes_despues"),
        ("wms", "0024_despacho_fecha_usuario"),
    ]

    operations = [
        # AddField con auto_now_add sella la hora de la migración: se limpia y se toma
        # del movimiento de salida del despacho (#1250) donde existe.
        migrations.RunSQL(
            """
            UPDATE despachos SET created_at = NULL, usuario_id = NULL;
            UPDATE despachos d
               SET created_at = m.fecha_movimiento, usuario_id = m.usuario_id
              FROM (SELECT DISTINCT ON (despacho_id) despacho_id, fecha_movimiento, usuario_id
                      FROM movimientos_inventario WHERE despacho_id IS NOT NULL
                     ORDER BY despacho_id, id) m
             WHERE m.despacho_id = d.id;
            """,
            migrations.RunSQL.noop,
        ),
    ]
