"""Carga el catálogo c_ClaveUnidad del SAT desde un .xlsx.

    python manage.py import_sat_unidades ruta/catalogo-unidades-de-medida-sat.xlsx

Usa la hoja "Catálogo completo" (o la última) y busca la fila de encabezado que
empieza con "Clave". ``Nombre`` es la descripción (lo que va en el CFDI) y
``Estado`` que no empiece con "Vigente" queda ``activo=False``. Upsert por ``codigo``.
"""

import openpyxl
from django.core.management.base import BaseCommand, CommandError

from nucleo.models import SatClaveUnidad


class Command(BaseCommand):
    help = "Importa las claves de unidad del SAT (c_ClaveUnidad) desde un .xlsx"

    def add_arguments(self, parser):
        parser.add_argument("archivo")

    def handle(self, archivo, **options):
        try:
            libro = openpyxl.load_workbook(archivo, read_only=True)
        except Exception as exc:
            raise CommandError(f"No se pudo abrir {archivo}: {exc}")
        try:
            hoja = next((h for h in libro.worksheets if "completo" in h.title.lower()), libro.worksheets[-1])
            claves = self._leer(hoja)
        finally:
            libro.close()
        if not claves:
            raise CommandError("No se encontró la tabla: se espera una fila de encabezado que empiece con 'Clave'.")

        SatClaveUnidad.objects.bulk_create(
            claves.values(), batch_size=1000,
            update_conflicts=True, unique_fields=["codigo"], update_fields=["descripcion", "activo"],
        )
        activas = sum(c.activo for c in claves.values())
        self.stdout.write(self.style.SUCCESS(f"{len(claves)} unidades importadas ({activas} vigentes)."))

    @staticmethod
    def _leer(hoja):
        claves, cols = {}, None
        for fila in hoja.iter_rows(values_only=True):
            celdas = [str(c or "").strip() for c in fila]
            if cols is None:
                if celdas and celdas[0] == "Clave":
                    cols = {nombre: i for i, nombre in enumerate(celdas)}
                continue
            codigo = celdas[0]
            if not codigo:
                continue
            claves[codigo] = SatClaveUnidad(
                codigo=codigo[:10],
                descripcion=celdas[cols.get("Nombre", 1)][:255],
                activo=celdas[cols["Estado"]].startswith("Vigente") if "Estado" in cols else True,
            )
        return claves
