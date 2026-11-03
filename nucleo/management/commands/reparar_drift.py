from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import ProgrammingError


def _es_drift(exc):
    msg = str(exc)
    return "already exists" in msg or "does not exist" in msg


class Command(BaseCommand):
    help = (
        "Recorre las migraciones pendientes y las aplica en orden. "
        "Si una migración choca porque su tabla/columna ya existe (o ya no existe) en la BD "
        "(drift de esquema vs. django_migrations), la aplica operación por operación: "
        "corre las que no chocan, omite y reporta las que chocan, y la registra como aplicada."
    )

    def handle(self, *args, **kwargs):
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())

        if not plan:
            self.stdout.write(self.style.SUCCESS("Nada pendiente."))
            return

        omitidas = []
        for migration, _backward in plan:
            app, name = migration.app_label, migration.name
            self.stdout.write(f"--- {app}.{name} ---")
            try:
                call_command("migrate", app, name, verbosity=1)
            except ProgrammingError as exc:
                if not _es_drift(exc):
                    raise
                self.stdout.write(self.style.WARNING(f"  drift de esquema detectado: {str(exc).strip()}"))
                self.stdout.write("  aplicando operación por operación")
                omitidas += [(f"{app}.{name}", op, err) for op, err in self._aplicar_por_operacion(app, name)]

        if omitidas:
            self.stdout.write(self.style.WARNING(f"Operaciones omitidas por drift ({len(omitidas)}):"))
            for migracion, op, err in omitidas:
                self.stdout.write(self.style.WARNING(f"  {migracion} | {op} | {err}"))
        self.stdout.write(self.style.SUCCESS("Reparación de drift completa."))

    def _aplicar_por_operacion(self, app, name):
        """Corre cada operación en su propio savepoint: las que chocan por drift se
        revierten solas y se reportan; el resto se aplica. Regresa las omitidas."""
        executor = MigrationExecutor(connection)
        migration = executor.loader.get_migration(app, name)
        estado = executor.loader.project_state((app, name), at_end=False)
        omitidas = []
        with connection.schema_editor(atomic=migration.atomic) as editor:
            for operacion in migration.operations:
                antes = estado.clone()
                operacion.state_forwards(app, estado)
                diferidas = len(editor.deferred_sql)
                try:
                    with transaction.atomic(savepoint=True):
                        operacion.database_forwards(app, editor, antes, estado)
                        # El SQL diferido (FKs, índices) corre aquí para que su choque cuente en esta operación.
                        for sql in editor.deferred_sql[diferidas:]:
                            editor.execute(sql)
                except ProgrammingError as exc:
                    if not _es_drift(exc):
                        raise
                    omitidas.append((operacion.describe(), str(exc).strip()))
                    self.stdout.write(self.style.WARNING(f"  OMITIDA {operacion.describe()}: {str(exc).strip()}"))
                else:
                    self.stdout.write(f"  aplicada {operacion.describe()}")
                finally:
                    del editor.deferred_sql[diferidas:]
            executor.record_migration(app, name)
        return omitidas
