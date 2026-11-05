import django.db.models.deletion
from django.db import migrations, models
from django.db.models.functions import Upper


def ligar_lecturas_existentes(apps, schema_editor):
    """EPCs en mayúsculas y cada lectura ligada a la etiqueta impresa con su EPC."""
    RfidScan = apps.get_model("wms", "RfidScan")
    Detalle = apps.get_model("wms", "EtiquetaRFIDDetalle")

    RfidScan.objects.exclude(epc=Upper("epc")).update(epc=Upper("epc"))

    detalles = {
        d.epc: d
        for d in Detalle.objects.filter(
            epc__in=RfidScan.objects.filter(empresa__isnull=False).values("epc")
        ).select_related("impresion")
    }
    primera = {}
    ligadas = []
    for scan in RfidScan.objects.filter(epc__in=list(detalles), empresa__isnull=False):
        detalle = detalles[scan.epc]
        if detalle.impresion.empresa_id != scan.empresa_id:
            continue
        scan.etiqueta_id = detalle.pk
        ligadas.append(scan)
        if detalle.pk not in primera or scan.created_at < primera[detalle.pk]:
            primera[detalle.pk] = scan.created_at
    RfidScan.objects.bulk_update(ligadas, ["etiqueta"], batch_size=500)
    for detalle_id, cuando in primera.items():
        Detalle.objects.filter(
            pk=detalle_id, estado__in=("PENDIENTE", "IMPRESO")
        ).update(estado="LEIDO", leida_at=cuando)


class Migration(migrations.Migration):

    dependencies = [
        ("wms", "0019_surtido_al_crear_picking"),
    ]

    operations = [
        migrations.AddField(
            model_name="etiquetarfiddetalle",
            name="leida_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="rfidscan",
            name="etiqueta",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="lecturas",
                to="wms.etiquetarfiddetalle",
            ),
        ),
        migrations.RunPython(ligar_lecturas_existentes, migrations.RunPython.noop),
    ]
