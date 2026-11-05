"""Visibilidad de importes por departamento, común a pedidos y órdenes de compra.

Una sola implementación de la regla (superusuario → admin de empresa → claves de
departamento de sus roles); cada módulo decide qué conjunto de claves la satisface
(``ventas.services.pedido_field_filter_service``,
``compras.services.orden_compra_view_service``). Antes había una copia por módulo
y se separaron (EC-784).
"""

from seguridad.role_identity import claves_departamento_usuario


def tiene_claves_de_importes(user, claves_permitidas, claves=None) -> bool:
    """``True`` si ``user`` es superusuario, admin de empresa o tiene un rol activo
    con alguna de ``claves_permitidas``.

    ``claves``: las claves de departamento del usuario si quien llama ya las resolvió
    en esta petición (cuestan una consulta); con ``None`` se consultan aquí. El
    superusuario y el admin nunca las necesitan.
    """
    if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
        return True
    if claves is None:
        claves = claves_departamento_usuario(user)
    return bool(claves & claves_permitidas)


def claves_de_la_peticion(view):
    """Claves de departamento del usuario de la petición, resueltas una sola vez.

    Se guardan en la vista, que vive lo que la petición (mismo patrón que
    ``OrdenCompraViewSet.ver_montos``, #836), para que el alcance y la regla de
    importes no repitan la consulta. ``None`` para el superusuario y el admin de
    empresa: ninguna de las dos reglas las necesita y así no cuestan consulta.
    """
    if not hasattr(view, "_claves_departamento"):
        user = view.request.user
        if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
            view._claves_departamento = None
        else:
            view._claves_departamento = claves_departamento_usuario(user)
    return view._claves_departamento


def copia_para_quitar(data, anidados=None):
    """Copia ``data`` y las listas de dicts nombradas en ``anidados`` (recursivo:
    ``{"detalles": {"tallas": {}}}``), que es lo que se modifica al quitar importes;
    lo demás se comparte sin tocarse. Sin ``deepcopy``: si la copia fallaba, los
    filtros de pedido y OC devolvían los datos sin filtrar (EC-784).
    """
    copia = dict(data)
    for clave, sub in (anidados or {}).items():
        valor = copia.get(clave)
        if isinstance(valor, list):
            copia[clave] = [copia_para_quitar(x, sub) if isinstance(x, dict) else x for x in valor]
    return copia


def quitar_claves(d, claves) -> None:
    """Quita ``claves`` de ``d`` en sitio; ignora lo que no sea dict."""
    if not isinstance(d, dict):
        return
    for k in claves:
        d.pop(k, None)


def quitar_montos(bloque: dict, campos) -> None:
    """Quita ``campos`` de un bloque de KPI y de las filas de sus listas, en sitio."""
    quitar_claves(bloque, campos)
    for valor in bloque.values():
        if isinstance(valor, list):
            for fila in valor:
                quitar_claves(fila, campos)
