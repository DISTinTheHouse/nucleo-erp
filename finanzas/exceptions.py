"""Excepciones propias de ``finanzas``.

Módulo hoja: sólo depende de Django. Lo importan tanto ``finanzas.services.*``
como ``finanzas.api.views``, sin ciclo posible en ninguna dirección.
"""

from django.core.exceptions import ValidationError


class ErrorDeNegocio(ValidationError):
    """Regla de negocio violada por lo que mandó el cliente. Se traduce a HTTP 400.

    Los servicios de ``finanzas`` no pueden levantar la ``ValidationError`` de
    DRF —deben seguir siendo agnósticos del framework, y se invocan también desde
    comandos de management y desde el admin—, pero la de Django, a secas, no
    distingue una regla de negocio de un fallo interno: un ``Model.clean()``, un
    validador de campo o una librería de terceros levantan esa misma clase ante
    datos corruptos o un bug de configuración.

    Al heredar de ella se conserva todo lo que ya funcionaba —``message_dict``,
    ``messages``, ``code``, ``params``, y por tanto ``get_error_detail`` de DRF—
    y se gana lo único que faltaba: poder decir "esto sí es para el cliente".
    ``ErroresDeNegocioComo400Mixin`` convierte a 400 exclusivamente esta
    subclase; una ``ValidationError`` de Django que llegue por cualquier otra vía
    sigue su curso y sale como 500, que es lo correcto para un fallo interno.
    """


# SQLSTATE de Postgres que indican un choque con otra transacción, no un fallo
# del dato: el cliente puede reintentar la misma petición.
LOCK_NOT_AVAILABLE = "55P03"
DEADLOCK_DETECTED = "40P01"
CONCURRENCY_SQLSTATES = frozenset({LOCK_NOT_AVAILABLE, DEADLOCK_DETECTED})

CONCURRENT_OPERATION_MESSAGE = (
    "Otra operación está modificando estos registros en este momento. Intente de nuevo."
)


class ConcurrentOperationError(Exception):
    """Un servicio no pudo tomar sus bloqueos porque otra transacción ya los tiene.

    Igual que ``ErrorDeNegocio``, no depende de DRF: la frontera HTTP de finanzas
    (``ConcurrencyConflictsAs409Mixin``) la traduce a 409.
    """


def database_error_sqlstate(exc):
    """SQLSTATE del error del driver que Django envolvió en ``exc``, si lo hay.

    Django relanza los errores del driver ``from`` el original, así que el código
    vive en ``__cause__``: ``sqlstate`` en psycopg 3, ``pgcode`` en psycopg2.
    """
    cause = getattr(exc, "__cause__", None)
    return getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
