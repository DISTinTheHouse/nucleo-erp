"""Visibilidad de importes por departamento, común a pedidos y órdenes de compra.

Una sola implementación de la regla (superusuario → admin de empresa → claves de
departamento de sus roles); cada módulo decide qué conjunto de claves la satisface
(``ventas.services.pedido_field_filter_service``,
``compras.services.orden_compra_view_service``). Antes había una copia por módulo
y se separaron (EC-784).
"""

from typing import Mapping, NamedTuple

from seguridad.role_identity import claves_departamento_usuario


def tiene_claves_de_importes(user, claves_permitidas) -> bool:
    """``True`` si ``user`` es superusuario, admin de empresa o tiene un rol activo
    con alguna de ``claves_permitidas``. El superusuario y el admin no consultan sus
    claves; para el resto, ``claves_departamento_usuario`` las consulta una sola vez
    por petición (se guardan en el objeto usuario)."""
    if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
        return True
    return bool(claves_departamento_usuario(user) & claves_permitidas)


class Importes(NamedTuple):
    """Dónde hay importes en un documento serializado: ``campos`` de este nivel y,
    por clave, la estructura de cada fila de sus listas anidadas. La misma
    estructura dice qué se copia y qué se quita, así que no pueden separarse
    (EC-784): una lista anidada que se limpia es siempre una que se copió."""

    campos: tuple
    anidados: Mapping[str, "Importes"] = {}


def quitar_importes(data, estructura: Importes) -> None:
    """Quita en sitio los importes de ``data`` y de sus listas anidadas según
    ``estructura``; ignora lo que no sea dict."""
    if not isinstance(data, dict):
        return
    quitar_claves(data, estructura.campos)
    for clave, sub in estructura.anidados.items():
        for fila in data.get(clave) or []:
            quitar_importes(fila, sub)


def copia_para_quitar(data, estructura: Importes):
    """Copia ``data`` y las filas de las listas anidadas de ``estructura`` —lo que
    ``quitar_importes`` modifica—; lo demás se comparte sin tocarse. Sin
    ``deepcopy``: si la copia fallaba, los filtros de pedido y OC devolvían los
    datos sin filtrar (EC-784)."""
    copia = dict(data)
    for clave, sub in estructura.anidados.items():
        valor = copia.get(clave)
        if isinstance(valor, list):
            copia[clave] = [copia_para_quitar(x, sub) if isinstance(x, dict) else x for x in valor]
    return copia


def sin_importes(data, estructura: Importes):
    """Copia de ``data`` sin importes, sin modificar ``data``."""
    filtrado = copia_para_quitar(data, estructura)
    quitar_importes(filtrado, estructura)
    return filtrado


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
