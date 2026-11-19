"""Pedido maestro de una recepción (#calidad): de qué pedido(s) viene lo recibido.

- OC: ``OrdenCompra.pedidos`` (una OC de materia prima puede surtir varios) y
  ``OrdenCompra.pedido`` (el primero, por compatibilidad).
- OP (también la recepción de una SM de OP): ``OrdenProduccionPedido`` y
  ``OrdenProduccion.pedido`` (el principal).
- SM de OB/OR: el pedido de la OB u OR.

El principal va primero. Quien llama trae en ``select_related``
``RELACIONES_PEDIDO``; aquí solo se consultan las dos tablas de varios pedidos
(OC y OP), en bloque: dos consultas sin importar cuántas recepciones.
"""
from compras.models import OrdenCompra
from produccion.models import OrdenProduccionPedido

RELACIONES_PEDIDO = (
    "orden_compra__pedido", "op__pedido", "salida_maquila__ob__pedido", "salida_maquila__orden_r__pedido",
)


def pedido_principal(recepcion):
    sm = recepcion.salida_maquila
    for doc in (recepcion.orden_compra, recepcion.op, getattr(sm, "ob", None), getattr(sm, "orden_r", None)):
        if doc is not None and doc.pedido_id:
            return doc.pedido
    return None


def origen_folio(recepcion):
    """Folio del documento contra el que se recibió: SM, OC u OP."""
    if recepcion.salida_maquila_id:
        return recepcion.salida_maquila.folio
    if recepcion.orden_compra_id:
        return recepcion.orden_compra.folio
    if recepcion.op_id:
        return recepcion.op.folio_op
    return None


def pedidos_de_recepciones(recepciones):
    """``{recepcion_id: [{"id", "folio"}]}``, el principal primero."""
    recepciones = list(recepciones)
    oc_ids = {r.orden_compra_id for r in recepciones if r.orden_compra_id}
    op_ids = {r.op_id for r in recepciones if r.op_id}
    por_oc, por_op = {}, {}
    if oc_ids:
        for oc_id, pid, folio in OrdenCompra.pedidos.through.objects.filter(ordencompra_id__in=oc_ids).values_list(
            "ordencompra_id", "pedido_id", "pedido__folio"
        ).order_by("id"):
            por_oc.setdefault(oc_id, []).append((pid, folio))
    if op_ids:
        for op_id, pid, folio in OrdenProduccionPedido.objects.filter(op_id__in=op_ids).values_list(
            "op_id", "pedido_id", "pedido__folio"
        ).order_by("id"):
            por_op.setdefault(op_id, []).append((pid, folio))

    out = {}
    for r in recepciones:
        pares = []
        principal = pedido_principal(r)
        if principal is not None:
            pares.append((principal.pk, principal.folio))
        pares += por_oc.get(r.orden_compra_id, []) + por_op.get(r.op_id, [])
        vistos, lista = set(), []
        for pid, folio in pares:
            if pid not in vistos:
                vistos.add(pid)
                lista.append({"id": pid, "folio": folio})
        out[r.pk] = lista
    return out
