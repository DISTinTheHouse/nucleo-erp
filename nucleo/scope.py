"""Alcance por empresa: un solo criterio para todo el ERP (EC-816, #538).

Se trabaja en **una empresa a la vez**: la activa (``user.empresa``). Solo el
superusuario la cambia, con ``POST /api/v1/nucleo/cambiar-empresa/`` (EC-843), a
cualquier empresa activa. El M2M ``empresas`` no amplía lo que se lee o escribe
ni, por ahora, permite cambiarse de empresa. Ventas,
compras, producción, finanzas y auditoría ya seguían este criterio; inventarios
sumaba el M2M y por eso no cambiaba al cambiar de empresa activa.

El superusuario se resuelve en cada llamador (global salvo excepciones
documentadas, p. ej. producción #361). Producción y WMS, donde el superusuario
también se acota a la empresa activa, comparten ``visibles_en_empresa_activa``.
"""


def empresas_activas():
    """Empresas a las que se puede cambiar: las activas. Un solo criterio para lo
    que acepta ``cambiar-empresa``, lo que ofrece ``mis-empresas`` y a dónde
    ``/api/v1/usuarios/`` mueve a otro usuario (EC-843).

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


def visibles_en_empresa_activa(qs, user):
    """Acota ``qs`` (modelo con ``empresa`` y ``sucursal``) a la empresa activa.

    Predicado de producción (``produccion/scope.py``, #361, EC-880) y de WMS
    (``wms/scope.py``, EC-882): SIEMPRE la empresa de ``empresa_ids_lectura``,
    superusuario incluido; sin empresa no se ve nada. Dentro de la empresa,
    superusuario e ``is_admin_empresa`` ven todas las sucursales y el resto sólo
    las de ``user.sucursales_permitidas()`` (M2M ``sucursales`` +
    ``sucursal_default``), filtrando por la sucursal dueña del registro.

    No aplica ``select_related``/``prefetch_related`` ni orden.
    """
    empresa_ids = empresa_ids_lectura(user)
    if not empresa_ids:
        return qs.none()
    qs = qs.filter(empresa_id__in=empresa_ids)
    if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
        return qs
    return qs.filter(sucursal_id__in=user.sucursales_permitidas())
