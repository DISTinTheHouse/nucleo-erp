"""Alcance de las entidades de producción: queryset base + aislamiento multi-tenant.

Mismo patrón que ``ventas/scope.py`` y ``terceros/scope.py``: vive aquí, y no
incrustado en los ViewSets, porque cada predicado tiene dos consumidores —su ViewSet
y el buscador global (``nucleo.api.search``)— y con una sola definición no pueden
separarse:

- ``ordenes_bordado_*``: ``OrdenBordadoViewSet`` + buscador.
- ``ordenes_reflejante_*``: ``OrdenReflejanteViewSet`` + buscador.
- ``ordenes_corte_manga_*``: ``OrdenesCorteMangaViewSet`` + buscador.

Hoy los tres predicados aplican el mismo criterio (son copias): cambiar la
política de uno exige revisar los otros dos.

Ninguna función aplica ``select_related``/``prefetch_related`` ni orden: eso sigue
siendo responsabilidad de cada consumidor.
"""

from produccion.models import OrdenesBordado, OrdenesCorteManga, OrdenesReflejante


def ordenes_bordado_base():
    """Filas existentes de ``produccion.OrdenesBordado``: excluye las borradas (soft delete)."""
    return OrdenesBordado.objects.filter(activo=True)


def ordenes_bordado_visibles(qs, user):
    """Alcance de ``produccion.OrdenesBordado``: empresa + sucursal.

    El superusuario ve todo; sin empresa no se ve nada; dentro de la empresa,
    ``is_admin_empresa`` ve todas las sucursales y el resto sólo las de
    ``user.sucursales_permitidas()`` (M2M ``sucursales`` + ``sucursal_default``).
    Mismo criterio que ``PickingViewSet``/``PackingViewSet``/``DespachoViewSet``/
    ``TransferenciaViewSet``.
    """
    if getattr(user, "is_superuser", False):
        return qs
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none()
    qs = qs.filter(empresa=empresa)
    if getattr(user, "is_admin_empresa", False):
        return qs
    return qs.filter(sucursal_id__in=user.sucursales_permitidas())


def ordenes_reflejante_base():
    """Filas existentes de ``produccion.OrdenesReflejante``: excluye las borradas (soft delete)."""
    return OrdenesReflejante.objects.filter(activo=True)


def ordenes_reflejante_visibles(qs, user):
    """Alcance de ``produccion.OrdenesReflejante``: empresa + sucursal.

    Réplica exacta del que tenía ``OrdenReflejanteViewSet.get_queryset()``, que es
    el mismo criterio que la orden de bordado: el superusuario ve todo; sin
    empresa no se ve nada; dentro de la empresa, ``is_admin_empresa`` ve todas las
    sucursales y el resto sólo las de ``user.sucursales_permitidas()``.
    """
    if getattr(user, "is_superuser", False):
        return qs
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none()
    qs = qs.filter(empresa=empresa)
    if getattr(user, "is_admin_empresa", False):
        return qs
    return qs.filter(sucursal_id__in=user.sucursales_permitidas())


def ordenes_corte_manga_base():
    """Filas existentes de ``produccion.OrdenesCorteManga``: excluye las borradas (soft delete)."""
    return OrdenesCorteManga.objects.filter(activo=True)


def ordenes_corte_manga_visibles(qs, user):
    """Alcance de ``produccion.OrdenesCorteManga``: empresa + sucursal.

    Réplica exacta del que tenía ``OrdenesCorteMangaViewSet.get_queryset()``, el
    mismo criterio que bordado y reflejante.
    """
    if getattr(user, "is_superuser", False):
        return qs
    empresa = getattr(user, "empresa", None)
    if not empresa:
        return qs.none()
    qs = qs.filter(empresa=empresa)
    if getattr(user, "is_admin_empresa", False):
        return qs
    return qs.filter(sucursal_id__in=user.sucursales_permitidas())
