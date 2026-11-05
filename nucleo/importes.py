"""Visibilidad de importes por departamento, común a pedidos y órdenes de compra.

Una sola implementación de la regla (superusuario → admin de empresa → claves de
departamento de sus roles); cada módulo decide qué conjunto de claves la satisface
(``ventas.services.pedido_field_filter_service``,
``compras.services.orden_compra_view_service``). Antes había una copia por módulo
y se separaron (EC-784).
"""

from seguridad.role_identity import claves_departamento_usuario


def tiene_claves_de_importes(user, claves_permitidas) -> bool:
    """``True`` si ``user`` es superusuario, admin de empresa o tiene un rol activo
    con alguna de ``claves_permitidas``. El superusuario y el admin no consultan sus
    claves; para el resto, ``claves_departamento_usuario`` las consulta una sola vez
    por petición (se guardan en el objeto usuario)."""
    if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
        return True
    return bool(claves_departamento_usuario(user) & claves_permitidas)


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
