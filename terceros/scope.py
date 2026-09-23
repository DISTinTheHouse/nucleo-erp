"""Alcance de las entidades de terceros: queryset base + aislamiento multi-tenant.

Mismo motivo y misma forma que ``ventas.scope``: el predicado tiene dos consumidores
—el ViewSet de la entidad y el buscador global (``nucleo.api.search``)— y una sola
definición impide que las copias se separen.
"""

from terceros.models import Cliente
from seguridad.role_identity import (
    CLAVE_DEPARTAMENTO_MESA_CONTROL,
    usuario_tiene_clave_departamento,
)


def clientes_base():
    """Filas existentes de ``terceros.Cliente``: excluye las borradas (soft delete)."""
    return Cliente.objects.filter(activo=True)


def clientes_visibles(qs, user):
    """Alcance de ``terceros.Cliente``: empresa + ``vendedores`` si no es admin/mesa de control.

    El superusuario ve todo; dentro de la empresa, ``is_admin_empresa`` y Mesa de
    Control ven TODOS los clientes de la empresa (Mesa de Control atiende pedidos
    de cualquier vendedor, no solo los suyos — mismo criterio de rol que
    ``ventas.api.views.PedidoViewSet._require_mesa_control``); el resto sólo ve
    los clientes que tiene asignados por el M2M ``vendedores``; sin empresa no se
    ve nada.
    """
    if getattr(user, "is_superuser", False):
        return qs
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none()
    qs = qs.filter(empresa=empresa)
    if getattr(user, "is_admin_empresa", False):
        return qs
    if usuario_tiene_clave_departamento(user, CLAVE_DEPARTAMENTO_MESA_CONTROL, empresa=empresa):
        return qs
    return qs.filter(vendedores__id=getattr(user, "id", None))
