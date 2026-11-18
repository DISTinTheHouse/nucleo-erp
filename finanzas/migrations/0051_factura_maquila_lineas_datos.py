# Datos aparte del esquema: en Postgres, insertar líneas y crear el índice de la
# FK nueva en la misma transacción falla ("pending trigger events").
from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations

CERO = Decimal("0")
CENTAVO = Decimal("0.01")


def lineas_a_facturas_de_maquila(apps, schema_editor):
    """#1242/#1232: las facturas de maquila en Borrador nacieron sin líneas y por el
    total de la SM. Se les arman las líneas: SM de OB/OR, la SM completa; SM de OP,
    lo que llegó en su recepción (repartido por ``op_detalle``). El estatus de la SM
    no se toca: el total de la factura queda en lo que de verdad llegó."""
    FacturaProveedor = apps.get_model("finanzas", "FacturaProveedor")
    FacturaProveedorDetalle = apps.get_model("finanzas", "FacturaProveedorDetalle")
    RecepcionDetalle = apps.get_model("compras", "RecepcionDetalle")
    SalidaMaquilaDetalle = apps.get_model("produccion", "SalidaMaquilaDetalle")
    facturas = FacturaProveedor.objects.filter(
        salida_maquila__isnull=False, estatus="Borrador", factura_proveedor_detalles__isnull=True,
    ).select_related("salida_maquila")
    for factura in facturas:
        sm = factura.salida_maquila
        detalles_sm = list(SalidaMaquilaDetalle.objects.filter(salida=sm).order_by("id"))
        pares = []  # (renglón SM, cantidad, renglón de recepción)
        if sm.op_id is None:
            pares = [(d, d.cantidad, None) for d in detalles_sm if d.cantidad > 0]
        else:
            libre = {d.pk: d.cantidad for d in detalles_sm}
            for rd in RecepcionDetalle.objects.filter(recepcion_id=factura.recepcion_id).order_by("id"):
                resto = rd.cantidad_recibida
                for d in detalles_sm:
                    if resto <= 0:
                        break
                    if d.op_detalle_id != rd.orden_produccion_detalle_id or libre[d.pk] <= 0:
                        continue
                    toma = min(resto, libre[d.pk])
                    libre[d.pk] -= toma
                    resto -= toma
                    pares.append((d, toma, rd))
        lineas = []
        for d, cantidad, rd in pares:
            subtotal = (cantidad * d.precio_unitario).quantize(CENTAVO, ROUND_HALF_UP)
            lineas.append(FacturaProveedorDetalle(
                factura_proveedor=factura, salida_maquila_detalle=d, recepcion_detalle=rd,
                producto_id=getattr(rd, "producto_id", None), descripcion=(d.descripcion or "")[:255],
                cantidad=cantidad, precio_unitario=d.precio_unitario.quantize(CENTAVO, ROUND_HALF_UP),
                descuento=CERO, impuesto=CERO, subtotal=subtotal, total=subtotal,
            ))
        if not lineas:
            continue
        FacturaProveedorDetalle.objects.bulk_create(lineas)
        factura.subtotal = factura.total = sum((l.total for l in lineas), CERO)
        factura.descuento = factura.impuestos = CERO
        factura.save(update_fields=["subtotal", "total", "descuento", "impuestos"])


class Migration(migrations.Migration):

    dependencies = [
        ("finanzas", "0050_factura_maquila_lineas"),
    ]

    operations = [
        migrations.RunPython(lineas_a_facturas_de_maquila, migrations.RunPython.noop),
    ]
