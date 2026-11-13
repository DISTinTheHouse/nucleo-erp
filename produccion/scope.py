"""Alcance de las entidades de producción: queryset base + aislamiento multi-tenant.

Mismo patrón que ``ventas/scope.py`` y ``terceros/scope.py``: vive aquí, y no
incrustado en los ViewSets, porque cada predicado tiene dos consumidores —su ViewSet
y el buscador global (``nucleo.api.search``)— y con una sola definición no pueden
separarse:

- ``ordenes_bordado_*``: ``OrdenBordadoViewSet`` + buscador.
- ``ordenes_reflejante_*``: ``OrdenReflejanteViewSet`` + buscador.
- ``ordenes_corte_manga_*``: ``OrdenesCorteMangaViewSet`` + buscador.

Los cuatro predicados aplican el mismo criterio (EC-880): SIEMPRE la empresa
activa del usuario (``user.empresa``), superusuario incluido; cambiar la política
de uno exige revisar los otros tres.

Los cuatro delegan en ``nucleo.scope.visibles_en_empresa_activa``, el mismo
predicado que WMS.

Ninguna función aplica ``select_related``/``prefetch_related`` ni orden: eso sigue
siendo responsabilidad de cada consumidor.
"""

from nucleo.scope import visibles_en_empresa_activa
from produccion.models import OrdenesBordado, OrdenesCorteManga, OrdenesReflejante, OrdenProduccion


def ordenes_produccion_base():
    """Filas existentes de ``produccion.OrdenProduccion``: excluye las borradas (soft delete)."""
    return OrdenProduccion.objects.filter(activo=True)


def ordenes_produccion_visibles(qs, user):
    """Alcance de ``produccion.OrdenProduccion``: SIEMPRE la empresa activa
    del usuario (``user.empresa``), incluido el superusuario. Desde EC-880
    bordado/reflejante/corte de manga usan el mismo criterio.

    Se decidió así en #361: ``OrdenProduccionViewSet.get_queryset()``
    (list/detail de OP) nunca le dio alcance global al superusuario, solo
    filtra por ``empresa``. Darle a ``kpis`` el criterio de OB/OR/OCM dejaba
    la tarjeta de KPIs y la tabla de OPs de la misma pantalla sin cuadrar
    para un superusuario con empresa asignada. Las dos fuentes deben usar el
    mismo criterio; se igualó ``kpis`` a list/detail, no al revés.

    Sin empresa (cualquier usuario, incluido superusuario) no se ve nada.
    Dentro de la empresa: superusuario e ``is_admin_empresa`` ven todas las
    sucursales, el resto solo las de ``user.sucursales_permitidas()``.
    """
    return visibles_en_empresa_activa(qs, user)


def ordenes_bordado_base():
    """Filas existentes de ``produccion.OrdenesBordado``: excluye las borradas (soft delete)."""
    return OrdenesBordado.objects.filter(activo=True)


def ordenes_bordado_visibles(qs, user):
    """Alcance de ``produccion.OrdenesBordado``: empresa activa + sucursal.

    SIEMPRE ``user.empresa``, superusuario incluido (EC-880: antes el
    superusuario veía, editaba y borraba órdenes de todas las empresas). Sin
    empresa no se ve nada; dentro de la empresa, superusuario e
    ``is_admin_empresa`` ven todas las sucursales y el resto sólo las de
    ``user.sucursales_permitidas()`` (M2M ``sucursales`` + ``sucursal_default``).
    Mismo criterio que ``ordenes_produccion_visibles``.
    """
    return visibles_en_empresa_activa(qs, user)


def ordenes_reflejante_base():
    """Filas existentes de ``produccion.OrdenesReflejante``: excluye las borradas (soft delete)."""
    return OrdenesReflejante.objects.filter(activo=True)


def ordenes_reflejante_visibles(qs, user):
    """Alcance de ``produccion.OrdenesReflejante``: empresa activa + sucursal.

    Mismo criterio que la orden de bordado (EC-880): siempre ``user.empresa``,
    superusuario incluido; sin empresa no se ve nada; dentro de la empresa,
    superusuario e ``is_admin_empresa`` ven todas las sucursales y el resto sólo
    las de ``user.sucursales_permitidas()``.
    """
    return visibles_en_empresa_activa(qs, user)


def ordenes_corte_manga_base():
    """Filas existentes de ``produccion.OrdenesCorteManga``: excluye las borradas (soft delete)."""
    return OrdenesCorteManga.objects.filter(activo=True)


def ordenes_corte_manga_visibles(qs, user):
    """Alcance de ``produccion.OrdenesCorteManga``: empresa activa + sucursal.

    Mismo criterio que bordado y reflejante (EC-880): siempre ``user.empresa``,
    superusuario incluido.
    """
    return visibles_en_empresa_activa(qs, user)
