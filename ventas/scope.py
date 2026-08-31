"""Alcance de las entidades de ventas: queryset base + aislamiento multi-tenant.

Vive aquí, y no incrustado en el ViewSet, porque tiene dos consumidores: el ViewSet
propio de cada entidad y el buscador global (``nucleo.api.search``). Mantener dos
copias es justo la clase de fuga cross-tenant que este repo ya arrastró; con una
sola definición no pueden separarse.

Se exponen dos cosas por entidad:

- ``*_base()``: qué filas EXISTEN (soft delete, etc.). También es el ``queryset`` de
  clase del ViewSet, para que no haya dos definiciones de lo mismo.
- ``*_visibles(qs, user)`` / ``alcance_*(qs, user)``: qué filas VE este usuario.

Ninguna aplica filtros de UI (``?q=``, ``estatus``, ``mis_pedidos``), anotaciones ni
orden: eso sigue siendo responsabilidad del ViewSet.
"""

from ventas.models import Cotizacion, Pedido


def pedidos_base():
    """Filas existentes de ``ventas.Pedido``: excluye las borradas (soft delete)."""
    return Pedido.objects.filter(activo=True)


def cotizaciones_base():
    """Filas existentes de ``ventas.Cotizacion`` (el modelo no tiene soft delete)."""
    return Cotizacion.objects.all()


def pedidos_visibles(qs, user):
    """Alcance de ``ventas.Pedido``: empresa directa.

    El superusuario ve todo aunque tenga empresa asignada; sin empresa no se ve nada
    (se falla cerrado).
    """
    if getattr(user, "is_superuser", False):
        return qs
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none()
    return qs.filter(empresa=empresa)


def alcance_cotizaciones(qs, user):
    """``(queryset, con_alcance)`` de ``ventas.Cotizacion``.

    El superusuario ve todo; dentro de la empresa, quien no es ``is_admin_empresa``
    sólo ve las cotizaciones de las que es vendedor; sin empresa no se ve nada.

    Devuelve también ``con_alcance`` para que el ViewSet no tenga que volver a
    evaluar la misma condición por su cuenta: necesita saberlo para salir antes de
    ``_apply_filters()``, que valida ``?estatus=`` y devolvería 400 donde hoy
    devuelve ``200 []`` a quien no puede ver nada. Con dos funciones separadas, un
    cambio futuro del alcance dejaría las dos respuestas descoordinadas.
    """
    if getattr(user, "is_superuser", False):
        return qs, True
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none(), False
    qs = qs.filter(empresa=empresa)
    if not getattr(user, "is_admin_empresa", False):
        qs = qs.filter(vendedor=user)
    return qs, True


def cotizaciones_visibles(qs, user):
    """Sólo el queryset de ``alcance_cotizaciones``, para quien no necesita el flag."""
    qs, _ = alcance_cotizaciones(qs, user)
    return qs
