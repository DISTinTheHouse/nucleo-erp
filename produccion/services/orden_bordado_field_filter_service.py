from __future__ import annotations


def armar_pedido_vinculado(orden):
    """``{id, folio}`` del pedido madre vinculado a una OT de producción.

    Helper compartido por serializers retrieve (OrdenBordado, por ahora) para
    no repetir la forma ``{pedido_id, pedido_folio}``. Si en un futuro la UI
    necesita más datos del pedido (cliente, fecha, etc.) se agregan keys dentro
    de este dict, sin crear campos planos nuevos ni romper el contrato.
    """
    pedido = getattr(orden, "pedido", None)
    if not pedido:
        return None
    return {"id": pedido.pk, "folio": str(getattr(pedido, "folio", pedido.pk) or pedido.pk)}
