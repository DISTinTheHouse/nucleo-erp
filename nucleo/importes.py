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
