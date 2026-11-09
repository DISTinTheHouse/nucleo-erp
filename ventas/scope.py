"""Alcance de las entidades de ventas: queryset base + aislamiento multi-tenant.

Vive aquí, y no incrustado en el ViewSet, porque tiene dos consumidores: el ViewSet
propio de cada entidad y el buscador global (``nucleo.api.search``). Mantener dos
copias es justo la clase de fuga cross-tenant que este repo ya arrastró; con una
sola definición no pueden separarse.

Se exponen dos cosas por entidad:

- ``*_base()``: qué filas EXISTEN (soft delete, etc.). También es el ``queryset`` de
  clase del ViewSet, para que no haya dos definiciones de lo mismo.
- ``*_visibles(qs, user)`` / ``alcance_*(qs, user)``: qué filas VE este usuario.

El superusuario opera en su empresa activa, como cualquier usuario (EC-798, en
línea con EC-816 y producción #361): ve toda la empresa —sin el sub-alcance por
vendedor— pero nunca otras. Sin empresa activa no ve nada.

Ninguna aplica filtros de UI (``?q=``, ``estatus``, ``mis_pedidos``), anotaciones ni
orden: eso sigue siendo responsabilidad del ViewSet.
"""

from ventas.models import Cotizacion, Oportunidad, Pedido


def _ve_toda_la_empresa(user):
    """Superusuario y admin de empresa ven toda su empresa; el resto, lo suyo."""
    return bool(getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False))


def pedidos_base():
    """Filas existentes de ``ventas.Pedido``: excluye las borradas (soft delete)."""
    return Pedido.objects.filter(activo=True)


def cotizaciones_base():
    """Filas existentes de ``ventas.Cotizacion`` (el modelo no tiene soft delete)."""
    return Cotizacion.objects.all()


def pedidos_visibles(qs, user):
    """Alcance de ``ventas.Pedido``: empresa directa.

    Solo la empresa activa, superusuario incluido (EC-798); sin empresa no se ve
    nada (se falla cerrado).
    """
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none()
    return qs.filter(empresa=empresa)


def alcance_cotizaciones(qs, user):
    """``(queryset, con_alcance)`` de ``ventas.Cotizacion``.

    Solo la empresa activa (superusuario incluido, EC-798); dentro de ella, quien
    no es superusuario ni ``is_admin_empresa`` sólo ve las cotizaciones de las que es vendedor; sin empresa no se ve nada.

    Devuelve también ``con_alcance`` para que el ViewSet no tenga que volver a
    evaluar la misma condición por su cuenta: necesita saberlo para salir antes de
    ``_apply_filters()``, que valida ``?estatus=`` y devolvería 400 donde hoy
    devuelve ``200 []`` a quien no puede ver nada. Con dos funciones separadas, un
    cambio futuro del alcance dejaría las dos respuestas descoordinadas.
    """
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none(), False
    qs = qs.filter(empresa=empresa)
    if not _ve_toda_la_empresa(user):
        qs = qs.filter(vendedor=user)
    return qs, True


def cotizaciones_visibles(qs, user):
    """Sólo el queryset de ``alcance_cotizaciones``, para quien no necesita el flag."""
    qs, _ = alcance_cotizaciones(qs, user)
    return qs


def oportunidades_base():
    """Filas existentes de ``ventas.Oportunidad``: excluye las dadas de baja."""
    return Oportunidad.objects.filter(activo=True)


def oportunidades_visibles(qs, user):
    """Alcance de ``ventas.Oportunidad``: el vendedor ve las suyas, el admin de empresa todas.

    Solo la empresa activa, superusuario incluido (EC-798); sin empresa no se ve
    nada (se falla cerrado).
    """
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none()
    qs = qs.filter(empresa=empresa)
    if not _ve_toda_la_empresa(user):
        qs = qs.filter(vendedor=user)
    return qs
