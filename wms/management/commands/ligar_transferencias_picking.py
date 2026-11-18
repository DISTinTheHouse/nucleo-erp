from django.core.management.base import BaseCommand

from auditoria.models import AuditoriaEvento
from wms.models import Picking, Transferencia
from wms.services import transferencia_picking_service as servicio


class Command(BaseCommand):
    help = (
        "Liga con su picking las TR que quedaron con Transferencia.picking en null "
        "(EC-916). Idempotente: solo toca TR en null y solo las que no dejan duda. "
        "El pipeline lo corre tras cada despliegue."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Solo informa qué ligaría, sin escribir.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            asignaciones, ambiguas = servicio.planear(Transferencia, Picking, AuditoriaEvento)
            ligadas = len(asignaciones)
        else:
            ligadas, ambiguas = servicio.ligar(Transferencia, Picking, AuditoriaEvento)
        prefijo = "[dry-run] " if options["dry_run"] else ""
        self.stdout.write(self.style.SUCCESS(f"{prefijo}TR ligadas: {ligadas}"))
        if ambiguas:
            self.stdout.write(f"{prefijo}TR sin ligar por ambiguas: {len(ambiguas)} (ids: {ambiguas})")
