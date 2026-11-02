from django.db import migrations

# Línea por código de categoría (hoja de líneas, 2026-11). TELAS y AVIOS quedan sin línea.
LINEAS = {
    "INDUSTRIAL": ["100", "101", "105", "200", "600", "601", "303"],
    "EMPRESARIAL": ["103", "104"],
    "ROPA_FRIO": ["300", "301", "305", "306", "307"],
    "EJECUTIVO": ["106", "109"],
    "SERVICIOS": ["107", "500", "501", "602", "603", "604", "605", "606", "607", "608"],
    "EPP": ["204", "302", "402", "700", "701", "702", "703", "704", "705", "706", "707", "708", "901"],
    "OTROS": [
        "102", "108", "201", "202", "203", "205", "304", "308", "400", "401",
        "502", "503", "504", "505", "506", "507", "508",
        "800", "801", "802", "803", "804", "805", "900", "902",
    ],
}
# Camisa, Blusa y Pantalón caen en Industrial y en Ejecutivo: la versión ejecutiva es
# otra categoría, con las tallas y procesos de la base.
EJECUTIVAS = [("100", "110", "Camisa ejecutiva"), ("101", "111", "Blusa ejecutiva"), ("200", "206", "Pantalon ejecutivo")]


def asignar_lineas(apps, schema_editor):
    Categoria = apps.get_model("catalogo", "CategoriaProducto")
    Talla = apps.get_model("catalogo", "CategoriaProductoTalla")
    Proceso = apps.get_model("catalogo", "CategoriaProductoProceso")
    for linea, codigos in LINEAS.items():
        Categoria.objects.filter(codigo__in=codigos, linea="").update(linea=linea)
    for base_codigo, codigo, nombre in EJECUTIVAS:
        for base in Categoria.objects.filter(codigo=base_codigo):
            if Categoria.objects.filter(empresa_id=base.empresa_id, codigo=codigo).exists():
                continue
            nueva = Categoria.objects.create(
                empresa_id=base.empresa_id, nombre=nombre, codigo=codigo, descripcion=nombre,
                unidad_medida_id=base.unidad_medida_id, linea="EJECUTIVO", activo=base.activo,
            )
            Talla.objects.bulk_create(
                Talla(categoria_producto=nueva, talla_id=t) for t in
                Talla.objects.filter(categoria_producto=base).values_list("talla_id", flat=True)
            )
            Proceso.objects.bulk_create(
                Proceso(empresa_id=p.empresa_id, categoria_producto=nueva, proceso=p.proceso, ubicacion=p.ubicacion)
                for p in Proceso.objects.filter(categoria_producto=base, activo=True)
            )


class Migration(migrations.Migration):

    dependencies = [
        ('catalogo', '0032_categoria_linea'),
    ]

    operations = [
        migrations.RunPython(asignar_lineas, migrations.RunPython.noop),
    ]
