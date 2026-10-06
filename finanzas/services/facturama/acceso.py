"""Quién puede usar la cuenta de Facturama.

Hay UNA sola cuenta de Facturama (un emisor, credenciales en ``settings``) y la BD
es multi-tenant: sin este candado, cualquier usuario autenticado de cualquier
empresa podía timbrar con el RFC de la empresa dueña, tocar su catálogo de
productos o descargar el XML de cualquier CFDI de la cuenta (incluidos recibidos y
nómina) con sólo conocer su id.

La regla:

1. Superusuario -> sí (staff global).
2. La empresa activa del usuario debe ser la dueña de la cuenta, configurada con
   ``FACTURAMA_EMPRESA_CODIGO`` (``Empresa.codigo``). Sin esa variable nadie más que
   el superusuario entra: falla cerrado.
3. Dentro de esa empresa, ``tiene_permiso(CLAVE_PERMISO_FACTURAMA)``: el admin de
   empresa pasa siempre; el resto necesita la clave por rol o GRANT.
"""

from django.conf import settings

CLAVE_PERMISO_FACTURAMA = "R-FIN-FACTURAMA"


def codigo_empresa_facturama():
    return (getattr(settings, "FACTURAMA_EMPRESA_CODIGO", "") or "").strip()


def empresa_usa_facturama(empresa):
    """``True`` sólo para la empresa dueña de la cuenta de Facturama."""
    codigo = codigo_empresa_facturama()
    return bool(codigo) and empresa is not None and getattr(empresa, "codigo", None) == codigo


def puede_usar_facturama(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    if not empresa_usa_facturama(getattr(user, "empresa", None)):
        return False
    return user.tiene_permiso(CLAVE_PERMISO_FACTURAMA)
