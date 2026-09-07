from django.db import migrations


def normaliza_vacios_a_null(apps, schema_editor):
    """Convierte '' en NULL para los campos únicos de Empleado.

    ``curp``, ``rfc`` y ``nss`` son ``unique=True, null=True, blank=True``.
    Postgres trata '' como un valor real dentro de un índice UNIQUE, así que un
    segundo empleado con el campo vacío choca contra el primero; en cambio
    admite tantos NULL como haga falta. Por eso el vacío correcto para estos
    tres campos es NULL y no ''.

    Se usa ``.update()`` masivo a propósito: ``Empleado.save()`` sincroniza
    ``fecha_baja`` según la transición de ``activo``, y esa lógica no debe
    dispararse durante la migración de datos.
    """
    Empleado = apps.get_model('hr', 'Empleado')
    db_alias = schema_editor.connection.alias

    for campo in ('curp', 'rfc', 'nss'):
        (
            Empleado.objects.using(db_alias)
            .filter(**{campo: ''})
            .update(**{campo: None})
        )


class Migration(migrations.Migration):

    dependencies = [
        ('hr', '0005_alter_incidencia_fecha_reporte_and_more'),
    ]

    operations = [
        # El reverso es un noop deliberado: una vez normalizados, los NULL de
        # estos campos son indistinguibles entre los que ya venían vacíos y los
        # que esta migración convirtió. Devolverlos a '' reintroduciría la
        # violación de unicidad en cuanto existiera más de una fila vacía.
        migrations.RunPython(normaliza_vacios_a_null, migrations.RunPython.noop),
    ]
