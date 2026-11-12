from __future__ import annotations


#: Campo de la foto del cliente en ``Pedido`` -> campo vivo en ``terceros.Cliente``.
_CLIENTE_SNAPSHOT = (
    ("nombre", "cliente_nombre"),
    ("razon_social", "cliente_razon_social"),
    ("rfc", "cliente_rfc"),
)


def cliente_de_pedido(pedido):
    """``{id, nombre, razon_social, rfc}`` del cliente de un pedido (EC-880).

    En bloque, nunca dato por dato (mezclar dos fuentes podía armar un cliente
    que no existe): si el pedido tiene la foto del cliente (``cliente_nombre``,
    ``cliente_razon_social``, ``cliente_rfc``; basta con que uno tenga valor),
    los tres salen de la foto, lo que se vendió, sin tocar ``pedido.cliente``
    (0 consultas). Sin foto, los tres salen del ``Cliente`` vivo; el llamador
    debe traer ``pedido__cliente`` en ``select_related``. ``id`` es siempre
    ``pedido.cliente_id``: la foto no guarda id. ``None`` sin pedido.
    """
    if pedido is None:
        return None
    foto = {campo: getattr(pedido, columna, None) for campo, columna in _CLIENTE_SNAPSHOT}
    if any(foto.values()):
        origen = foto
    else:
        cliente = pedido.cliente if pedido.cliente_id else None
        origen = {campo: getattr(cliente, campo, None) for campo, _ in _CLIENTE_SNAPSHOT}
    return {"id": pedido.cliente_id, **{campo: valor or None for campo, valor in origen.items()}}


def armar_pedido_vinculado(orden):
    """``{id, folio, cliente}`` del pedido madre vinculado a una OT de producción.

    Helper compartido por los serializers retrieve de OP/OB/OR/OCM para no
    repetir la forma ``{pedido_id, pedido_folio}``. Si la UI necesita más datos
    del pedido se agregan keys dentro de este dict, sin crear campos planos
    nuevos ni romper el contrato. ``cliente`` (EC-880): ver ``cliente_de_pedido``.
    """
    pedido = getattr(orden, "pedido", None)
    if not pedido:
        return None
    return {
        "id": pedido.pk,
        "folio": str(getattr(pedido, "folio", pedido.pk) or pedido.pk),
        "cliente": cliente_de_pedido(pedido),
    }
