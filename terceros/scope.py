"""Alcance de las entidades de terceros: queryset base + aislamiento multi-tenant.

Mismo motivo y misma forma que ``ventas.scope``: el predicado tiene dos consumidores
—el ViewSet de la entidad y el buscador global (``nucleo.api.search``)— y una sola
definición impide que las copias se separen.
"""

from terceros.models import Cliente
from seguridad.role_identity import (
    CLAVE_DEPARTAMENTO_CONTABILIDAD,
    CLAVE_DEPARTAMENTO_MESA_CONTROL,
    claves_departamento_usuario,
)

# Ven todos los clientes de su empresa, no solo los asignados: Mesa de Control
# atiende pedidos de cualquier vendedor y Contabilidad factura, cobra y emite
# notas de crédito a cualquier cliente.
DEPARTAMENTOS_TODOS_LOS_CLIENTES = frozenset({CLAVE_DEPARTAMENTO_MESA_CONTROL, CLAVE_DEPARTAMENTO_CONTABILIDAD})


def clientes_base():
    """Filas existentes de ``terceros.Cliente``: excluye las borradas (soft delete)."""
    return Cliente.objects.filter(activo=True)


def ve_clientes_sin_asignacion(user):
    """``True`` para quien ``clientes_visibles`` nunca acota por asignación ni por
    departamento: el superusuario y el admin de empresa.

    Son las dos primeras salidas de ``clientes_visibles``; si esa precedencia cambia,
    cambia aquí también. Mesa de Control no entra: ve todos los clientes de su empresa,
    pero por departamento (cuesta una consulta) y lo resuelve ``clientes_visibles``.
    """
    return bool(getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False))


def clientes_visibles_entre(user, ids):
    """Cuáles de ``ids`` puede ver ``user`` como cliente, o ``None`` si los ve todos.

    Sirve para decidir si se muestra el nombre de un cliente ligado a otro registro.
    El superusuario y el admin de empresa los ven todos, también los dados de baja, y
    no cuestan consulta. Al resto se le aplica ``clientes_visibles`` acotado a ``ids``:
    una sola consulta, para llamarla una vez por respuesta y no por fila.
    """
    if ve_clientes_sin_asignacion(user):
        return None
    ids = {i for i in ids if i is not None}
    if not ids:
        return frozenset()
    return frozenset(
        clientes_visibles(clientes_base(), user).filter(pk__in=ids).values_list("pk", flat=True)
    )


def clientes_visibles(qs, user):
    """Alcance de ``terceros.Cliente``: empresa + ``vendedores`` si no es admin/mesa de control/contabilidad.

    El superusuario ve todo; dentro de la empresa, ``is_admin_empresa``, Mesa de
    Control y Contabilidad ven TODOS los clientes de la empresa (Mesa de Control atiende pedidos
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
    if claves_departamento_usuario(user, empresa=empresa) & DEPARTAMENTOS_TODOS_LOS_CLIENTES:
        return qs
    return qs.filter(vendedores__id=getattr(user, "id", None))
