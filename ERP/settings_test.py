"""Settings de pruebas: ``manage.py test`` contra un Postgres LOCAL, nunca Supabase.

Uso::

    python manage.py test --settings=ERP.settings_test --parallel --keepdb [apps]

Hereda todo de ``ERP.settings`` y reemplaza la base de datos por la local
(``LOCAL_POSTGRES_*`` del ``.env``), sin importar ``USE_REMOTE_DB``. Django crea la base
de pruebas (``test_erp_pruebas`` y sus clones ``_1``, ``_2``... con ``--parallel``) en
ese servidor a partir de ``template1``, que debe tener ``pg_trgm`` (los índices
``gin_trgm_ops`` la usan y ninguna migración la crea).

Se niega a arrancar si el host no es local: un ``manage.py test`` sin estos settings
creó ``test_postgres`` en Supabase (2026-08-03). SQLite tampoco: el proyecto usa SQL y
tipos propios de Postgres.
"""

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403
from .settings import (
    LOCAL_POSTGRES_DB_HOST,
    LOCAL_POSTGRES_DB_NAME,
    LOCAL_POSTGRES_DB_PASSWORD,
    LOCAL_POSTGRES_DB_PORT,
    LOCAL_POSTGRES_DB_USER,
    LOCAL_POSTGRES_SSLMODE,
)

HOSTS_LOCALES = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}

if LOCAL_POSTGRES_DB_HOST not in HOSTS_LOCALES:
    raise ImproperlyConfigured(
        f"ERP.settings_test sólo corre contra un Postgres local; LOCAL_POSTGRES_DB_HOST="
        f"{LOCAL_POSTGRES_DB_HOST!r} no lo es. Nunca se prueba contra Supabase."
    )

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        # Base "de trabajo" a la que Django se conecta para crear/borrar la de pruebas;
        # las pruebas nunca escriben en ella.
        "NAME": LOCAL_POSTGRES_DB_NAME,
        "USER": LOCAL_POSTGRES_DB_USER,
        "PASSWORD": LOCAL_POSTGRES_DB_PASSWORD,
        "HOST": LOCAL_POSTGRES_DB_HOST,
        "PORT": LOCAL_POSTGRES_DB_PORT,
        "OPTIONS": {"sslmode": LOCAL_POSTGRES_SSLMODE},
        "TEST": {"NAME": "test_erp_pruebas"},
    }
}

# Hash rápido: crear usuarios en ``setUpTestData`` con PBKDF2 cuesta ~0.3 s cada uno.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
