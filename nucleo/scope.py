"""Alcance por empresa: un solo criterio para todo el ERP (EC-816, #538).

Se trabaja en **una empresa a la vez**: la activa (``user.empresa``). Solo el
superusuario la cambia, con ``POST /api/v1/nucleo/cambiar-empresa/`` (EC-843), a
cualquier empresa activa. El M2M ``empresas`` no amplía lo que se lee o escribe
ni, por ahora, permite cambiarse de empresa. Ventas,
compras, producción, finanzas y auditoría ya seguían este criterio; inventarios
sumaba el M2M y por eso no cambiaba al cambiar de empresa activa.

El superusuario se resuelve en cada llamador (global salvo excepciones
documentadas, p. ej. producción #361).
"""


def empresas_activas():
    """Empresas a las que se puede cambiar: las activas. Un solo criterio para lo
    que acepta ``cambiar-empresa`` y lo que ofrece ``mis-empresas`` (EC-843).

    QuerySet perezoso: no consulta la BD hasta evaluarse.
    """
    from nucleo.models import Empresa

    return Empresa.objects.filter(activo=True)


def empresa_ids_lectura(user):
    """Empresas cuyos datos puede ver/escribir el usuario: solo la activa.

    Lista vacía si no tiene empresa activa (el llamador debe fallar cerrado).
    No consulta la BD.
    """
    empresa_id = getattr(user, "empresa_id", None)
    return [empresa_id] if empresa_id else []


def es_empresa_activa(user, empresa_id):
    """``True`` si ``empresa_id`` es la empresa activa del usuario."""
    return bool(empresa_id) and empresa_id == getattr(user, "empresa_id", None)
