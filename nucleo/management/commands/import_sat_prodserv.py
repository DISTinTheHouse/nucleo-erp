"""Carga el catálogo c_ClaveProdServ del SAT desde su Excel oficial.

    python manage.py import_sat_prodserv ruta/catalogo-productos-servicios-sat.xlsx

Upsert por ``codigo``: se puede volver a correr con un catálogo nuevo. Una clave
con ``FechaFinVigencia`` queda ``activo=False`` (los endpoints solo listan activas).
"""

import openpyxl
from django.core.management.base import BaseCommand, CommandError

from nucleo.models import SatClaveProdServ

LOTE = 2000


class Command(BaseCommand):
    help = "Importa las claves de producto/servicio del SAT (c_ClaveProdServ) desde un .xlsx"

    def add_arguments(self, parser):
        parser.add_argument("archivo")

    def handle(self, archivo, **options):
        try:
            libro = openpyxl.load_workbook(archivo, read_only=True)
        except Exception as exc:
            raise CommandError(f"No se pudo abrir {archivo}: {exc}")
        try:
            claves = self._leer(libro.worksheets[0])
        finally:
            libro.close()
        if not claves:
            raise CommandError("El archivo no trae claves: se espera c_ClaveProdServ en la primera columna.")

        SatClaveProdServ.objects.bulk_create(
            claves.values(), batch_size=LOTE,
            update_conflicts=True, unique_fields=["codigo"], update_fields=["descripcion", "activo"],
        )
        activas = sum(c.activo for c in claves.values())
        self.stdout.write(self.style.SUCCESS(f"{len(claves)} claves importadas ({activas} vigentes)."))

    @staticmethod
    def _leer(hoja):
        filas = hoja.iter_rows(values_only=True)
        encabezado = [str(c or "").strip() for c in next(filas, ())]
        try:
            fin = encabezado.index("FechaFinVigencia")
        except ValueError:
            fin = None

        claves = {}
        for fila in filas:
            codigo = str(fila[0] or "").strip()
            if not codigo.isdigit():
                continue
            claves[codigo] = SatClaveProdServ(
                codigo=codigo,
                descripcion=str(fila[1] or "").strip()[:255],
                activo=fin is None or not fila[fin],
            )
        return claves
