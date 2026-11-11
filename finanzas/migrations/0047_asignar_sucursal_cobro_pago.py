"""Asigna sucursal a los cobros y pagos que existían sin ella (#1063).

Cobro: la de la factura de su primera línea. Pago: la de la factura de proveedor
de su primera línea. Sin líneas (o sin sucursal en la factura): la primera
sucursal de su empresa. Reverso: no hace nada.
"""
from django.db import migrations


def asignar(apps, schema_editor):
    Cobro = apps.get_model("finanzas", "Cobro")
    CobroDetalle = apps.get_model("finanzas", "CobroDetalle")
    Pago = apps.get_model("finanzas", "Pago")
    PagoDetalle = apps.get_model("finanzas", "PagoDetalle")
    Sucursal = apps.get_model("nucleo", "Sucursal")

    primera = {}
    for empresa_id, suc_id in Sucursal.objects.order_by("empresa_id", "pk").values_list("empresa_id", "pk"):
        primera.setdefault(empresa_id, suc_id)

    por_cobro = {}
    for cobro_id, suc_id in (
        CobroDetalle.objects.filter(cobro__sucursal__isnull=True, cxc__factura__sucursal__isnull=False)
        .order_by("cobro_id", "pk").values_list("cobro_id", "cxc__factura__sucursal_id")
    ):
        por_cobro.setdefault(cobro_id, suc_id)
    for cobro in Cobro.objects.filter(sucursal__isnull=True).only("pk", "empresa_id"):
        suc = por_cobro.get(cobro.pk) or primera.get(cobro.empresa_id)
        if suc:
            Cobro.objects.filter(pk=cobro.pk).update(sucursal_id=suc)

    por_pago = {}
    for pago_id, suc_id in (
        PagoDetalle.objects.filter(pago__sucursal__isnull=True, cxp__factura_proveedor__sucursal__isnull=False)
        .order_by("pago_id", "pk").values_list("pago_id", "cxp__factura_proveedor__sucursal_id")
    ):
        por_pago.setdefault(pago_id, suc_id)
    for pago in Pago.objects.filter(sucursal__isnull=True).only("pk", "empresa_id"):
        suc = por_pago.get(pago.pk) or primera.get(pago.empresa_id)
        if suc:
            Pago.objects.filter(pk=pago.pk).update(sucursal_id=suc)


class Migration(migrations.Migration):

    dependencies = [
        ("finanzas", "0046_cobro_pago_sucursal"),
    ]

    operations = [migrations.RunPython(asignar, migrations.RunPython.noop)]
