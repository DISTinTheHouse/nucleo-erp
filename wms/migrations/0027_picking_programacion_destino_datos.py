from django.db import migrations

# Pickings anteriores: el destino se infiere del almacén destino, el mismo
# ``almacen_sugerido`` que el onboarding propone por destino (``ALMACEN_POR_DESTINO``:
# APARTADO -> APARTADOS, BORDADO -> BORDADOS). Si no coincide, queda nulo y no cuenta.
SQL = """
UPDATE picking p
   SET programacion_destino = CASE UPPER(a.nombre)
                                  WHEN 'APARTADOS' THEN 'APARTADO'
                                  WHEN 'BORDADOS' THEN 'BORDADO'
                              END
  FROM almacenes a
 WHERE a.id_almacen = p.almacen_destino_id
   AND p.programacion_destino IS NULL
   AND UPPER(a.nombre) IN ('APARTADOS', 'BORDADOS');
"""


class Migration(migrations.Migration):

    dependencies = [
        ("wms", "0026_picking_programacion_destino"),
    ]

    operations = [migrations.RunSQL(SQL, migrations.RunSQL.noop)]
