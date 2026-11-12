from __future__ import annotations


#: Campo de la foto del cliente en ``Pedido`` -> campo vivo en ``terceros.Cliente``.
_CLIENTE_SNAPSHOT = (
    ("nombre", "cliente_nombre"),
    ("razon_social", "cliente_razon_social"),
    ("rfc", "cliente_rfc"),
)


def cliente_de_pedido(pedido):
    """``{id, nombre, razon_social, rfc}`` del cliente de un pedido (EC-880).

    Cada dato sale de la foto que guarda el pedido (``cliente_nombre``,
    ``cliente_razon_social``, ``cliente_rfc``): lo que se vendió. Sólo si la
    foto de ese dato está vacía se lee el ``Cliente`` vivo. Con la foto completa
    no se toca ``pedido.cliente`` (0 consultas); para el respaldo, el llamador
    debe traer ``pedido__cliente`` en ``select_related``. ``None`` sin pedido.
    """
    if pedido is None:
        return None
    datos = {"id": pedido.cliente_id}
    cliente = None
    for campo, foto in _CLIENTE_SNAPSHOT:
        valor = getattr(pedido, foto, None)
        if not valor and pedido.cliente_id:
            if cliente is None:
                cliente = pedido.cliente
            valor = getattr(cliente, campo, None)
        datos[campo] = valor or None
    return datos


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
