from django.db import migrations


class Migration(migrations.Migration):
    """``productos.descripcion`` quedó nullable en la BD (el modelo no lo es) y
    6,303 filas traían NULL (y también ``updated_at``). ``catalogo_historicalproducto.descripcion`` sí es
    NOT NULL, así que editar cualquiera de esas filas tronaba con 500 al
    escribir el historial. Solo datos; el ``SET NOT NULL`` va en 0037."""

    dependencies = [
        ("catalogo", "0035_producto_es_compras"),
    ]

    operations = [
        migrations.RunSQL(
            "UPDATE productos SET descripcion = '' WHERE descripcion IS NULL",
            migrations.RunSQL.noop,
        ),
        # Mismas filas sin ``updated_at`` (carga masiva): se toma ``created_at``.
        migrations.RunSQL(
            "UPDATE productos SET updated_at = created_at WHERE updated_at IS NULL",
            migrations.RunSQL.noop,
        ),
    ]
