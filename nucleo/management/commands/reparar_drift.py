from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import ProgrammingError


class Command(BaseCommand):
    help = (
        "Recorre las migraciones pendientes y las aplica en orden. "
        "Si una migración falla porque su tabla/columna ya existe en la BD "
        "(drift de esquema vs. django_migrations), la marca con --fake y sigue."
    )

    def handle(self, *args, **kwargs):
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())

        if not plan:
            self.stdout.write(self.style.SUCCESS("Nada pendiente."))
            return

        for migration, _backward in plan:
            app, name = migration.app_label, migration.name
            self.stdout.write(f"--- {app}.{name} ---")
            try:
                call_command("migrate", app, name, verbosity=1)
            except ProgrammingError as exc:
                msg = str(exc)
                if "already exists" not in msg and "does not exist" not in msg:
                    raise
                self.stdout.write(self.style.WARNING("  drift de esquema detectado, aplicando --fake"))
                call_command("migrate", app, name, fake=True, verbosity=1)

        self.stdout.write(self.style.SUCCESS("Reparación de drift completa."))
