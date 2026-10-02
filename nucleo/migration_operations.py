"""Operaciones de migración compartidas por las apps del proyecto."""

from django.db import migrations


class AddIndexSoloPostgres(migrations.AddIndex):
    """``AddIndex`` que sólo crea el índice en PostgreSQL.

    Existe por los índices de EXPRESIÓN con opclass del buscador global
    (``OpClass(Upper("col"), name="text_pattern_ops" | "gin_trgm_ops")``). En
    PostgreSQL son los que sirven a ``UPPER("col"::text) LIKE UPPER(%s)``, que es
    como Django compila ``istartswith``/``icontains``. Pero SQLite —donde corren
    las pruebas— no conoce los opclasses y el DDL revienta (``near
    "text_pattern_ops": syntax error``). Los índices v1 sobre columnas simples no
    tenían el problema porque ahí Django descarta el opclass en SQLite; con una
    expresión, no.

    El ESTADO sí se actualiza en todos los backends, así que ``Meta.indexes`` y
    ``makemigrations --check`` siguen coincidiendo. Sólo se salta el DDL.
    """

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            super().database_backwards(app_label, schema_editor, from_state, to_state)

    def describe(self):
        return f"{super().describe()} (sólo PostgreSQL)"
