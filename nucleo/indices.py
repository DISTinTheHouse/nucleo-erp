"""Índices que sólo existen en PostgreSQL.

Los índices del buscador global son de EXPRESIÓN con opclass
(``OpClass(Upper("col"), name="text_pattern_ops" | "gin_trgm_ops")``): en
PostgreSQL son los que sirven a ``UPPER("col"::text) LIKE UPPER(%s)``, que es como
Django compila ``istartswith``/``icontains``. SQLite —donde corren las pruebas— no
conoce los opclasses y el DDL revienta (``near "text_pattern_ops": syntax error``).
Con un campo simple Django descarta el opclass en SQLite; con una expresión, no.

La guarda vive en el ÍNDICE, no en la operación de migración, para que cubra todos
los caminos por los que el schema editor emite su DDL: ``AddIndex``/``RemoveIndex``
que genere ``makemigrations`` en el futuro, el ``CreateModel`` y la reconstrucción
de tablas de SQLite (``_remake_table``, que recrea los ``Meta.indexes``). Un
``RenameIndex`` en SQLite (``can_rename_index = False``) se ejecuta como
``remove_index`` + ``add_index``, que pasan por aquí; en PostgreSQL es un
``ALTER INDEX ... RENAME`` sobre un índice que sí existe.

Fuera de PostgreSQL ``create_sql``/``remove_sql`` devuelven una sentencia VACÍA, no
``None``: ``BaseDatabaseSchemaEditor.execute()`` hace ``str(sql)`` sin comprobar
nada, así que ``None`` se ejecutaría como el texto ``"None"``. Una sentencia vacía
es un no-op en el driver.
"""

from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.backends.ddl_references import Statement


def _es_postgres(schema_editor):
    return schema_editor.connection.vendor == "postgresql"


def _sentencia_vacia():
    return Statement("")


class _SoloPostgresMixin:
    def create_sql(self, model, schema_editor, using="", **kwargs):
        if not _es_postgres(schema_editor):
            return _sentencia_vacia()
        return super().create_sql(model, schema_editor, using=using, **kwargs)

    def remove_sql(self, model, schema_editor, **kwargs):
        if not _es_postgres(schema_editor):
            return _sentencia_vacia()
        return super().remove_sql(model, schema_editor, **kwargs)


class IndexSoloPostgres(_SoloPostgresMixin, models.Index):
    """``models.Index`` (btree) que no emite DDL fuera de PostgreSQL."""


class GinIndexSoloPostgres(_SoloPostgresMixin, GinIndex):
    """``GinIndex`` que no emite DDL fuera de PostgreSQL."""
