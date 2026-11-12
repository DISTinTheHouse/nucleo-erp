"""#1121: recalcula ``subtotal_talla``/``subtotal_linea`` de pedidos y cotizaciones
(siempre se guardaban en 0). #1118: copia el motivo de los pedidos ya cancelados
desde su evento de auditoría ``CANCELAR``."""

from django.db import migrations
from django.db.models import DecimalField, F, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

_DEC = DecimalField(max_digits=12, decimal_places=2)


def _recalcular(Detalle, Talla, fk):
    precio = Detalle.objects.filter(pk=OuterRef(fk)).values("precio_unitario")[:1]
    Talla.objects.update(subtotal_talla=F("cantidad") * Coalesce("precio_unitario", Subquery(precio), output_field=_DEC))
    suma = Talla.objects.filter(**{fk: OuterRef("pk")}).values(fk).annotate(s=Sum("subtotal_talla")).values("s")[:1]
    Detalle.objects.update(subtotal_linea=Coalesce(Subquery(suma, output_field=_DEC), 0, output_field=_DEC))


def adelante(apps, schema_editor):
    _recalcular(apps.get_model("ventas", "PedidoDetalle"), apps.get_model("ventas", "PedidoDetalleTalla"), "pedido_detalle")
    _recalcular(apps.get_model("ventas", "CotizacionDetalle"), apps.get_model("ventas", "CotizacionDetalleTalla"), "cotizacion_detalle")
    Pedido = apps.get_model("ventas", "Pedido")
    Evento = apps.get_model("auditoria", "AuditoriaEvento")
    eventos = Evento.objects.filter(modulo="ventas", accion="CANCELAR", tabla="pedidos").order_by("created_at")
    for ev in eventos.values("id_registro", "despues_json"):
        motivo = (ev["despues_json"] or {}).get("motivo")
        if motivo and str(ev["id_registro"]).isdigit():
            Pedido.objects.filter(pk=int(ev["id_registro"]), motivo_cancelacion__isnull=True).update(
                motivo_cancelacion=str(motivo)[:500])


class Migration(migrations.Migration):
    dependencies = [
        ("ventas", "0062_motivo_cancelacion_pedido"),
        ("auditoria", "0007_bitacora_etiquetas_uniformes"),
    ]
    operations = [migrations.RunPython(adelante, migrations.RunPython.noop)]
