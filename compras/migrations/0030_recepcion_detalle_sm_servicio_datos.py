"""Recepciones de SM de OB/OR hechas antes de contar piezas por renglón: se
recibían completas y sin renglones. Se les agregan los renglones de la SM por
su cantidad completa, para que lo recibido de esas SM cuadre (no hay faltantes).
Solo cuenta piezas: no toca inventario, facturas ni el estatus de la SM."""
from django.db import migrations


def agregar_renglones(apps, schema_editor):
    Recepcion = apps.get_model("compras", "Recepcion")
    RecepcionDetalle = apps.get_model("compras", "RecepcionDetalle")
    SalidaMaquilaDetalle = apps.get_model("produccion", "SalidaMaquilaDetalle")

    recepciones = (
        Recepcion.objects.filter(salida_maquila__isnull=False, salida_maquila__op__isnull=True)
        .exclude(recepciondetalle__isnull=False)
    )
    nuevos = []
    for recepcion in recepciones:
        for d in SalidaMaquilaDetalle.objects.filter(salida_id=recepcion.salida_maquila_id, cantidad__gt=0).select_related(
            "ob_detalle", "or_detalle"
        ):
            origen = d.ob_detalle or d.or_detalle
            nuevos.append(RecepcionDetalle(
                recepcion_id=recepcion.pk, salida_maquila_detalle_id=d.pk,
                producto_id=getattr(origen, "producto_id", None), cantidad_recibida=d.cantidad,
            ))
    RecepcionDetalle.objects.bulk_create(nuevos)


class Migration(migrations.Migration):

    dependencies = [
        ("compras", "0029_recepcion_detalle_sm_servicio"),
        ("produccion", "0060_salida_maquila_desde_or"),
    ]

    operations = [migrations.RunPython(agregar_renglones, migrations.RunPython.noop)]
