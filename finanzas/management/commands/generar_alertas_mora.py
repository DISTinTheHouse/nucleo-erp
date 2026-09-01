from django.core.management.base import BaseCommand
from finanzas.services.alerta_mora_service import AlertaMoraService


class Command(BaseCommand):
    help = "Genera o refresca las alertas de mora para CxC y CxP vencidas."

    def add_arguments(self, parser):
        parser.add_argument(
            "--empresa",
            type=int,
            default=None,
            help="Id de empresa a procesar (opcional, por defecto todas).",
        )

    def handle(self, *args, **options):
        total = AlertaMoraService.generar_alertas(empresa=options.get("empresa"))
        self.stdout.write(self.style.SUCCESS(f"Alertas generadas: {total}"))
