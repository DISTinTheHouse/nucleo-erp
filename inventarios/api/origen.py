"""Documento de origen de un renglón de movimiento de inventario.

Una sola regla para los reportes de movimientos y el historial por SKU:
``origen_tipo`` (PEDIDO, RECEPCION, TRANSFERENCIA, AJUSTE, OP, PICKING,
DEVOLUCION) y ``origen_folio`` siempre llenos. Los folios se anotan en la misma
consulta (``anotar_origen``): nada de recorrer relaciones por fila.

Precedencia (el documento más específico gana):

1. Recepción: RECEPCION; si es SALIDA, es una devolución a proveedor → DEVOLUCION.
2. Transferencia: PICKING si la generó un picking (folio del picking), si no
   TRANSFERENCIA.
3. Despacho de WMS: PICKING (folio del picking del packing despachado).
4. Orden de producción: OP (``folio_op``).
5. Devolución de cliente: DEVOLUCION (``DEV-<id>``; el modelo no lleva folio).
6. Pedido (salida manual o de factura ligada a pedido): PEDIDO.
7. Factura sin pedido (reintegro/redescuento): DEVOLUCION con el folio de la factura.
8. Ajuste: AJUSTE (``AJ-<id>``: ``AjusteInventario`` no tiene folio; mismo formato que el auxiliar).
9. Sin documento: AJUSTE con ``MOV-<id del movimiento>``.
"""
import re

from django.db.models import F, Q

M = "movimiento_inventario__"

#: nombre corto -> ruta desde ``MovimientoInventarioDetalle``.
CAMPOS_ORIGEN = {
    "recepcion_id": M + "recepcion_id",
    "recepcion_folio": M + "recepcion__folio",
    "transferencia_id": M + "transferencia_id",
    "transferencia_folio": M + "transferencia__folio",
    "tr_picking_folio": M + "transferencia__picking__folio",
    "despacho_id": M + "despacho_id",
    "despacho_picking_folio": M + "despacho__packing__picking__folio",
    "op_id": M + "op_id",
    "op_folio": M + "op__folio_op",
    "devolucion_id": M + "devolucion_id",
    "pedido_id": M + "pedido_id",
    "pedido_folio": M + "pedido__folio",
    "factura_id": M + "factura_id",
    "factura_folio": M + "factura__folio",
    "ajuste_id": M + "ajuste_inventario_id",
}

TIPOS_ORIGEN = ("PEDIDO", "RECEPCION", "TRANSFERENCIA", "AJUSTE", "OP", "PICKING", "DEVOLUCION")


def anotar_origen(qs):
    """Anota ``o_<campo>`` de ``CAMPOS_ORIGEN`` (LEFT JOINs en la misma consulta)."""
    return qs.annotate(**{f"o_{k}": F(v) for k, v in CAMPOS_ORIGEN.items()})


def nombres_valores():
    """Nombres de las anotaciones, para ``.values(...)``."""
    return [f"o_{k}" for k in CAMPOS_ORIGEN]


def resolver_origen(tipo_movimiento, movimiento_id, o):
    """``(origen_tipo, origen_folio, origen_id)``. ``o(campo)`` lee el valor anotado."""
    if o("recepcion_id"):
        tipo = "DEVOLUCION" if tipo_movimiento == "SALIDA" else "RECEPCION"
        return tipo, o("recepcion_folio") or f"REC-{o('recepcion_id')}", o("recepcion_id")
    if o("transferencia_id"):
        if o("tr_picking_folio"):
            return "PICKING", o("tr_picking_folio"), o("transferencia_id")
        return "TRANSFERENCIA", o("transferencia_folio") or f"TR-{o('transferencia_id')}", o("transferencia_id")
    if o("despacho_id"):
        return "PICKING", o("despacho_picking_folio") or f"DES-{o('despacho_id')}", o("despacho_id")
    if o("op_id"):
        return "OP", o("op_folio") or f"OP-{o('op_id')}", o("op_id")
    if o("devolucion_id"):
        return "DEVOLUCION", f"DEV-{o('devolucion_id')}", o("devolucion_id")
    if o("pedido_id"):
        return "PEDIDO", o("pedido_folio") or f"PED-{o('pedido_id')}", o("pedido_id")
    if o("factura_id"):
        return "DEVOLUCION", o("factura_folio") or f"FAC-{o('factura_id')}", o("factura_id")
    if o("ajuste_id"):
        return "AJUSTE", f"AJ-{o('ajuste_id')}", o("ajuste_id")
    return "AJUSTE", f"MOV-{movimiento_id}", movimiento_id


_PREFIJOS = {"AJ": M + "ajuste_inventario_id", "DEV": M + "devolucion_id", "MOV": "movimiento_inventario_id"}
_FOLIOS = (
    "recepcion_folio", "transferencia_folio", "tr_picking_folio", "despacho_picking_folio",
    "op_folio", "pedido_folio", "factura_folio",
)


def filtro_referencia(referencia):
    """``Q`` por folio exacto (sin distinguir mayúsculas) de cualquier documento,
    o por los folios sintéticos ``AJ-<id>``, ``DEV-<id>``, ``MOV-<id>``."""
    referencia = referencia.strip()
    q = Q()
    for campo in _FOLIOS:
        q |= Q(**{f"{CAMPOS_ORIGEN[campo]}__iexact": referencia})
    m = re.fullmatch(r"(?i)(AJ|DEV|MOV)-(\d+)", referencia)
    if m:
        q |= Q(**{_PREFIJOS[m.group(1).upper()]: int(m.group(2))})
    return q


def filtro_busqueda(texto):
    """``Q`` de la búsqueda libre ``q``: cada palabra debe aparecer en el SKU, el
    nombre del producto/variante, las observaciones o algún folio."""
    q = Q()
    for palabra in texto.split():
        q &= (
            Q(producto_variante__sku__icontains=palabra)
            | Q(variante_produccion__sku__icontains=palabra)
            | Q(producto_variante__nombre__icontains=palabra)
            | Q(producto_variante__producto__nombre__icontains=palabra)
            | Q(producto__nombre__icontains=palabra)
            | Q(**{M + "observaciones__icontains": palabra})
            | Q(**{CAMPOS_ORIGEN["pedido_folio"] + "__icontains": palabra})
            | Q(**{CAMPOS_ORIGEN["recepcion_folio"] + "__icontains": palabra})
            | Q(**{CAMPOS_ORIGEN["transferencia_folio"] + "__icontains": palabra})
            | Q(**{CAMPOS_ORIGEN["op_folio"] + "__icontains": palabra})
        )
    return q
