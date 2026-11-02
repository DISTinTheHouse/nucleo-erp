from django.db import migrations

NOMBRES = {"MP": "Materia prima", "PT": "Producto terminado", "SERVICIO": "Servicio", "COMPRAS": "Compras"}
# Categorías de materia prima (TELAS, AVIOS).
CATEGORIAS_MP = ("TEL", "AV")


def asignar_tipos(apps, schema_editor):
    """Nombra los tipos y da tipo a los productos que no tenían: TELAS y AVIOS → MP;
    el resto → PT (hoy sólo 2 productos de prueba QA-249)."""
    TipoProducto = apps.get_model("catalogo", "TipoProducto")
    Producto = apps.get_model("catalogo", "Producto")
    for codigo, nombre in NOMBRES.items():
        TipoProducto.objects.filter(codigo=codigo, nombre="").update(nombre=nombre)
    sin_tipo = Producto.objects.filter(tipo__isnull=True)
    if not sin_tipo.exists():
        return
    mp, _ = TipoProducto.objects.get_or_create(codigo="MP", defaults={"nombre": NOMBRES["MP"]})
    pt, _ = TipoProducto.objects.get_or_create(codigo="PT", defaults={"nombre": NOMBRES["PT"]})
    sin_tipo.filter(categoria_producto__codigo__in=CATEGORIAS_MP).update(tipo=mp)
    sin_tipo.update(tipo=pt)


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0029_tipo_producto_nombre_activo'),
    ]

    operations = [
        migrations.RunPython(asignar_tipos, migrations.RunPython.noop),
    ]
