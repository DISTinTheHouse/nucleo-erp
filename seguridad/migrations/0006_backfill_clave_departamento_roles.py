from django.db import migrations
import re


TOKEN_EQUIVALENCIAS = {
    "MESACONTROL": "MESACONTROL",
    "MESADECONTROL": "MESACONTROL",
    "MESACONTROLYVENTAS": "MESACONTROLYVENTAS",
    "CONTAVENTAS": "CONTAVENTAS",
    "CONTACOMPRAS": "CONTACOMPRAS",
    "VENTAS": "VENTAS",
    "COMPRAS": "COMPRAS",
    "CONTABILIDAD": "CONTABILIDAD",
    "CONTABILID": "CONTABILIDAD",
    "RH": "RH",
    "PRODUCCION": "PRODUCCION",
    "WMS": "WMS",
    "ALMACENMP": "WMS",
}

TOKEN_PREFIJOS = (
    ("MESACONTROLYVENTAS", "MESACONTROLYVENTAS"),
    ("MESACONTROL", "MESACONTROL"),
    ("MESADECONTROL", "MESACONTROL"),
    ("CONTAVENTAS", "CONTAVENTAS"),
    ("CONTACOMPRAS", "CONTACOMPRAS"),
    ("CONTABILIDAD", "CONTABILIDAD"),
    ("CONTABILID", "CONTABILIDAD"),
    ("COMPRAS", "COMPRAS"),
    ("PRODUCCION", "PRODUCCION"),
    ("ALMACENMP", "WMS"),
    ("VENTAS", "VENTAS"),
    ("WMS", "WMS"),
    ("RH", "RH"),
)


def normalizar_token_rol(value):
    try:
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    except Exception:
        return ""


def inferir_clave_departamento(*values):
    for value in values:
        token = normalizar_token_rol(value)
        if not token:
            continue
        mapped = TOKEN_EQUIVALENCIAS.get(token)
        if mapped:
            return mapped
        for prefijo, clave in TOKEN_PREFIJOS:
            if token.startswith(prefijo):
                return clave
    return ""


def backfill_clave_departamento(apps, schema_editor):
    Rol = apps.get_model("seguridad", "Rol")

    for rol in Rol.objects.all().only("id", "codigo", "nombre", "clave_departamento"):
        clave = inferir_clave_departamento(
            getattr(rol, "clave_departamento", None),
            getattr(rol, "nombre", None),
            getattr(rol, "codigo", None),
        )
        if not clave or clave == rol.clave_departamento:
            continue
        Rol.objects.filter(pk=rol.pk).update(clave_departamento=clave)


class Migration(migrations.Migration):

    dependencies = [
        ("seguridad", "0005_usuariopermiso"),
    ]

    operations = [
        migrations.RunPython(backfill_clave_departamento, migrations.RunPython.noop),
    ]
