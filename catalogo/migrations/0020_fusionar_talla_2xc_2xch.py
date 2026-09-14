"""Fusiona la talla duplicada ``2XCH`` (id=7) en ``2XC`` (id=13).

Negocio confirmó que ambas filas de ``tallas`` son la misma talla. Sobrevive
id=13 (la referencian las variantes de producto con CASCADE y renglones de
cotización/pedido con PROTECT) y se renombra a ``2XCH``; id=7 se elimina
después de reapuntar a id=13 todo lo que la referencia.

Las relaciones hacia ``Talla`` se descubren del estado de migraciones en vez
de listarlas a mano, para que también cubra las tablas ``Historical*``.

Solo actúa si las dos filas existen con los nombres esperados; en cualquier
otra base (SQLite de CI, entornos sin estos datos) es un no-op.
"""

from django.db import migrations, transaction

ID_DUPLICADA = 7
ID_SOBREVIVIENTE = 13
NOMBRE_DUPLICADA = "2XCH"
NOMBRE_SOBREVIVIENTE_ACTUAL = "2XC"
NOMBRE_FINAL = "2XCH"

# Renglones que representan "una talla dentro de un padre". Si un mismo padre
# ya tiene id=7 e id=13 a la vez, reapuntar crearía dos renglones de la misma
# talla; en lugar de adivinar cómo fusionarlos se aborta y se listan.
PADRES_POR_MODELO = {
    "ventas.cotizaciondetalletalla": "cotizacion_detalle_id",
    "ventas.pedidodetalletalla": "pedido_detalle_id",
    "produccion.ordenbordadodetalle": "pedido_detalle_id",
    "produccion.ordenreflejantedetalle": "pedido_detalle_id",
    "produccion.ordencortemangadetalle": "pedido_detalle_id",
}


def _relaciones_a_talla(apps):
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if (
                getattr(field, "concrete", False)
                and field.is_relation
                and not field.many_to_many
                and field.related_model is not None
                and field.related_model._meta.label_lower == "catalogo.talla"
            ):
                yield model, field


def fusionar_tallas(apps, schema_editor):
    Talla = apps.get_model("catalogo", "Talla")
    duplicada = Talla.objects.filter(pk=ID_DUPLICADA, nombre=NOMBRE_DUPLICADA).first()
    sobreviviente = Talla.objects.filter(
        pk=ID_SOBREVIVIENTE, nombre=NOMBRE_SOBREVIVIENTE_ACTUAL
    ).first()
    if duplicada is None or sobreviviente is None:
        return

    relaciones = list(_relaciones_a_talla(apps))

    with transaction.atomic():
        for model, field in relaciones:
            padre = PADRES_POR_MODELO.get(model._meta.label_lower)
            if not padre:
                continue
            colisiones = sorted(
                set(
                    model._base_manager.filter(**{field.attname: ID_DUPLICADA})
                    .values_list(padre, flat=True)
                )
                & set(
                    model._base_manager.filter(**{field.attname: ID_SOBREVIVIENTE})
                    .values_list(padre, flat=True)
                )
            )
            if colisiones:
                raise RuntimeError(
                    f"{model._meta.label}: {padre} con ambas tallas "
                    f"{ID_DUPLICADA} y {ID_SOBREVIVIENTE}: {colisiones}. "
                    "Fusión abortada; resolver esos renglones a mano."
                )

        for model, field in relaciones:
            model._base_manager.filter(**{field.attname: ID_DUPLICADA}).update(
                **{field.attname: ID_SOBREVIVIENTE}
            )

        restantes = {
            f"{model._meta.label}.{field.name}": n
            for model, field in relaciones
            if (n := model._base_manager.filter(**{field.attname: ID_DUPLICADA}).count())
        }
        if restantes:
            raise RuntimeError(
                f"Aún hay referencias a la talla {ID_DUPLICADA}: {restantes}"
            )

        Talla.objects.filter(pk=ID_SOBREVIVIENTE).update(nombre=NOMBRE_FINAL)
        borradas, _ = Talla.objects.filter(pk=ID_DUPLICADA).delete()
        if borradas != 1:
            raise RuntimeError(
                f"Se esperaba borrar 1 fila de tallas, se borraron {borradas}."
            )


def revertir_catalogo(apps, schema_editor):
    """Restaura solo el catálogo: renombra id=13 a ``2XC`` y recrea id=7.

    Los renglones reapuntados se quedan en id=13: la migración no guarda cuáles
    venían de id=7, así que no se pueden devolver con certeza.
    """
    Talla = apps.get_model("catalogo", "Talla")
    if not Talla.objects.filter(pk=ID_SOBREVIVIENTE, nombre=NOMBRE_FINAL).exists():
        return
    if Talla.objects.filter(pk=ID_DUPLICADA).exists():
        return
    Talla.objects.filter(pk=ID_SOBREVIVIENTE).update(nombre=NOMBRE_SOBREVIVIENTE_ACTUAL)
    Talla.objects.create(pk=ID_DUPLICADA, nombre=NOMBRE_DUPLICADA, activo=True)


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0019_historicalproductovariante_cod_proscai_and_more"),
        ("ventas", "0046_remove_historicalpedido_cantidad_embarque_and_more"),
        ("produccion", "0033_ordenbordadodetalle_tipos_servicio"),
    ]

    operations = [
        migrations.RunPython(fusionar_tallas, revertir_catalogo),
    ]
