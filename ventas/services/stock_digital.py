"""Stock digital: existencia comercial por SKU para Vendedores y Mesa de Control.

Solo lectura: no mueve inventario, no aparta piezas y no cambia cómo se autoriza
o se surte un pedido. Es una vista de lo que ya dicen existencias, pedidos, OP y
OC. Por variante de catálogo (SKU):

- **Existencia real**: ``Existencia.cantidad`` en el almacén de surtido
  ``ALM-00`` (Producto terminado). De ahí sale todo pedido (#694): lo que el
  picking ya pasó a apartados, embarque o bordado ya no está aquí.
- **Comprometido**: pedidos activos Autorizados o En proceso; por talla
  ``max(pedida − surtida por picking, 0)``, la regla de ``wms/api/surtido.py``
  (#1246, #1247). El picking saca la prenda de ``ALM-00`` (TR) y libera el
  compromiso a la vez, así que no se descuenta dos veces. Cancelar el pedido lo
  libera.
- **En producción**: OP activas y abiertas (Pendiente, Preparación, En
  producción, Revisión, Detenido); por renglón ``max(cantidad − recibido, 0)``.
- **En compra**: OC activas Autorizadas o Parcialmente recibidas; por renglón
  ``max(cantidad − recibido, 0)``.
  "Recibido" = renglones de recepciones activas y no canceladas, como en compras.
- **Disponible hoy** = real − comprometido.
- **Disponibilidad digital** = real + en producción + en compra − comprometido.
  Negativa = faltante: lo comprometido no alcanza ni con lo que viene.

Fechas: ``fecha_entrega_estimada`` de cada OP y OC (nula si no se capturó).
Fuera de alcance: muestras (sin SKU de catálogo) y tallas de pedido sin variante.

Costo: ``totales`` hace 4 consultas agrupadas sin importar cuántas variantes;
``detalle`` hace otras 4 para una variante.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import DecimalField, F, Min, Sum, Value
from django.db.models.functions import Greatest

from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
from inventarios.models import Existencia
from produccion.models import OrdenProduccion, OrdenProduccionDetalle
from ventas.models import Pedido, PedidoDetalleTalla
from ventas.services.clasificacion_service import rango_fecha_entrega
from wms.api.surtido import _picking_vivo, _suma

#: Almacén del que sale todo pedido (#694), el de ``PedidoViewSet.ALMACEN_SURTIDO``.
ALMACEN_SURTIDO = "ALM-00"

ESTATUS_PEDIDO_COMPROMETEN = (Pedido.ESTATUS_AUTORIZADA, Pedido.ESTATUS_EN_PROCESO)
_EOP = OrdenProduccion.EstatusOrdenProduccion
ESTATUS_OP_ABIERTAS = (_EOP.PENDIENTE, _EOP.PREPARACION, _EOP.BORDANDO, _EOP.REVISION, _EOP.DETENIDO)
_EOC = OrdenCompra.EstatusOrdenCompra
ESTATUS_OC_ABIERTAS = (_EOC.AUTORIZADA, _EOC.PARCIALMENTE_RECIBIDA)

#: Valores de ``?solo=`` del listado y la condición de cada uno.
FILTROS_SOLO = {
    "disponibles": lambda t: t["disponible_digital"] > 0,
    "faltantes": lambda t: t["disponible_digital"] < 0,
    "comprometidos": lambda t: t["comprometido"] > 0,
    "entradas": lambda t: t["en_produccion"] + t["en_compra"] > 0,
    "existencia": lambda t: t["existencia_real"] > 0,
}

NUM = DecimalField(max_digits=18, decimal_places=4)
CERO = Decimal("0")
_CERO = Value(CERO, output_field=NUM)


def _recepciones_vivas():
    return RecepcionDetalle.objects.filter(recepcion__activo=True).exclude(
        recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)


def existencias(empresa_id, sucursal_id=None):
    """Filas de ``Existencia`` con SKU en el ``ALM-00`` de la empresa (o de la sucursal)."""
    qs = Existencia.objects.filter(
        almacen__empresa_id=empresa_id, almacen__codigo=ALMACEN_SURTIDO, producto_variante__isnull=False,
    )
    if sucursal_id:
        qs = qs.filter(almacen__sucursal_id=sucursal_id)
    return qs


def tallas_comprometidas(empresa_id, sucursal_id=None):
    """Tallas con SKU de pedidos que comprometen, con ``surtida`` y ``pendiente``."""
    qs = PedidoDetalleTalla.objects.filter(
        variante__isnull=False,
        pedido_detalle__pedido__empresa_id=empresa_id,
        pedido_detalle__pedido__activo=True,
        pedido_detalle__pedido__estatus__in=ESTATUS_PEDIDO_COMPROMETEN,
    )
    if sucursal_id:
        qs = qs.filter(pedido_detalle__pedido__sucursal_id=sucursal_id)
    return qs.annotate(
        surtida=_suma(_picking_vivo(), "cantidad_surtida", "pedido_detalle_talla_id"),
    ).annotate(pendiente=Greatest(F("cantidad") - F("surtida"), _CERO, output_field=NUM))


def renglones_op(empresa_id, sucursal_id=None):
    """Renglones con SKU de OP abiertas con ``recibido`` y ``pendiente`` > 0."""
    qs = OrdenProduccionDetalle.objects.filter(
        producto_variante__isnull=False, activo=True,
        op__empresa_id=empresa_id, op__activo=True, op__estatus_op__in=ESTATUS_OP_ABIERTAS,
    )
    if sucursal_id:
        qs = qs.filter(op__sucursal_id=sucursal_id)
    return qs.annotate(
        recibido=_suma(_recepciones_vivas(), "cantidad_recibida", "orden_produccion_detalle_id"),
    ).annotate(pendiente=Greatest(F("cantidad") - F("recibido"), _CERO, output_field=NUM)).filter(pendiente__gt=0)


def renglones_oc(empresa_id, sucursal_id=None):
    """Renglones con SKU de OC autorizadas/parciales con ``recibido`` y ``pendiente`` > 0."""
    qs = OrdenCompraDetalle.objects.filter(
        producto_variante__isnull=False,
        orden_compra__empresa_id=empresa_id, orden_compra__activo=True,
        orden_compra__estatus__in=ESTATUS_OC_ABIERTAS,
    )
    if sucursal_id:
        qs = qs.filter(orden_compra__sucursal_id=sucursal_id)
    return qs.annotate(
        recibido=_suma(_recepciones_vivas(), "cantidad_recibida", "orden_compra_detalle_id"),
    ).annotate(pendiente=Greatest(F("cantidad") - F("recibido"), _CERO, output_field=NUM)).filter(pendiente__gt=0)


def _totales(real=CERO, comprometido=CERO, en_produccion=CERO, en_compra=CERO, fechas=()):
    fechas = [f for f in fechas if f]
    return {
        "existencia_real": real,
        "comprometido": comprometido,
        "en_produccion": en_produccion,
        "en_compra": en_compra,
        "disponible_hoy": real - comprometido,
        "disponible_digital": real + en_produccion + en_compra - comprometido,
        "proxima_entrada": min(fechas) if fechas else None,
    }


TOTALES_VACIOS = _totales()


def totales(empresa_id, variantes, sucursal_id=None):
    """``{variante_id: totales}`` de ``variantes`` (lista de ids o subconsulta de
    pks). Solo trae las que tienen algo; el resto es ``TOTALES_VACIOS``. 4 consultas."""

    def agrupar(qs, campo, total, fecha=None):
        anotaciones = {"t": Sum(total)}
        if fecha:
            anotaciones["f"] = Min(fecha)
        filas = qs.filter(**{f"{campo}__in": variantes}).order_by().values(campo).annotate(**anotaciones)
        return {r[campo]: (r["t"] or CERO, r.get("f")) for r in filas}

    real = agrupar(existencias(empresa_id, sucursal_id), "producto_variante_id", "cantidad")
    comp = agrupar(tallas_comprometidas(empresa_id, sucursal_id), "variante_id", "pendiente")
    prod = agrupar(renglones_op(empresa_id, sucursal_id), "producto_variante_id", "pendiente",
                   "op__fecha_entrega_estimada")
    compra = agrupar(renglones_oc(empresa_id, sucursal_id), "producto_variante_id", "pendiente",
                     "orden_compra__fecha_entrega_estimada")
    salida = {}
    for vid in set(real) | set(comp) | set(prod) | set(compra):
        p, fp = prod.get(vid, (CERO, None))
        c, fc = compra.get(vid, (CERO, None))
        salida[vid] = _totales(real.get(vid, (CERO,))[0], comp.get(vid, (CERO,))[0], p, c, (fp, fc))
    return salida


def _nombre(first, last):
    return f"{first or ''} {last or ''}".strip() or None


def detalle(empresa_id, variante_id, sucursal_id=None):
    """Totales de una variante y de dónde salen: existencia por almacén, pedidos
    que la comprometen y entradas de OP/OC con su fecha estimada. 4 consultas."""
    estatus_pedido = dict(Pedido.CHOICES_ESTATUS)

    existencia = [
        {
            "almacen_id": r["almacen_id"], "almacen": r["almacen__codigo"],
            "sucursal_id": r["almacen__sucursal_id"], "sucursal": r["almacen__sucursal__nombre"],
            "cantidad": r["total"] or CERO,
        }
        for r in existencias(empresa_id, sucursal_id).filter(producto_variante_id=variante_id).order_by()
        .values("almacen_id", "almacen__codigo", "almacen__sucursal_id", "almacen__sucursal__nombre")
        .annotate(total=Sum("cantidad")).order_by("almacen__sucursal_id", "almacen_id")
    ]

    p = "pedido_detalle__pedido__"
    compromisos = []
    for r in (
        tallas_comprometidas(empresa_id, sucursal_id).filter(variante_id=variante_id).order_by()
        .values(
            f"{p}id", f"{p}folio", f"{p}cliente_nombre", f"{p}estatus", f"{p}clasificacion",
            f"{p}fecha_clasificacion", f"{p}fecha_confirmacion", f"{p}created_at", f"{p}sucursal_id",
            f"{p}retenido", f"{p}cotizacion__vendedor__first_name", f"{p}cotizacion__vendedor__last_name",
        )
        .annotate(pedidas=Sum("cantidad"), surtidas=Sum("surtida"), pendientes=Sum("pendiente"))
        .filter(pendientes__gt=0)
    ):
        desde, hasta = rango_fecha_entrega(SimpleNamespace(
            clasificacion=r[f"{p}clasificacion"], fecha_clasificacion=r[f"{p}fecha_clasificacion"],
            fecha_confirmacion=r[f"{p}fecha_confirmacion"], created_at=r[f"{p}created_at"],
        ))
        compromisos.append({
            "pedido_id": r[f"{p}id"], "folio": r[f"{p}folio"], "cliente_nombre": r[f"{p}cliente_nombre"],
            "vendedor": _nombre(r[f"{p}cotizacion__vendedor__first_name"], r[f"{p}cotizacion__vendedor__last_name"]),
            "sucursal_id": r[f"{p}sucursal_id"],
            "estatus": r[f"{p}estatus"], "estatus_display": estatus_pedido.get(r[f"{p}estatus"]),
            "clasificacion": r[f"{p}clasificacion"], "retenido": r[f"{p}retenido"],
            "fecha_confirmacion": r[f"{p}fecha_confirmacion"],
            "entrega_desde": desde, "entrega_hasta": hasta,
            "pedida": Decimal(r["pedidas"] or 0), "surtida": r["surtidas"] or CERO, "pendiente": r["pendientes"],
        })
    # Primero lo que se entrega antes; sin fecha estimada al final.
    compromisos.sort(key=lambda c: (c["entrega_hasta"] or date.max, c["pedido_id"]))

    entradas_op = [
        {
            "op_id": r["op_id"], "folio": r["op__folio_op"], "sucursal_id": r["op__sucursal_id"],
            "estatus": r["op__estatus_op"], "estatus_display": _EOP(r["op__estatus_op"]).label,
            "pedido_folio": r["op__pedido__folio"], "fecha_entrega_estimada": r["op__fecha_entrega_estimada"],
            "cantidad": r["piezas"], "recibido": r["recibidas"], "pendiente": r["pendientes"],
        }
        for r in renglones_op(empresa_id, sucursal_id).filter(producto_variante_id=variante_id).order_by()
        .values("op_id", "op__folio_op", "op__sucursal_id", "op__estatus_op", "op__pedido__folio",
                "op__fecha_entrega_estimada")
        .annotate(piezas=Sum("cantidad"), recibidas=Sum("recibido"), pendientes=Sum("pendiente"))
    ]
    entradas_oc = [
        {
            "orden_compra_id": r["orden_compra_id"], "folio": r["orden_compra__folio"],
            "sucursal_id": r["orden_compra__sucursal_id"], "proveedor": r["orden_compra__proveedor__nombre"],
            "estatus": r["orden_compra__estatus"], "estatus_display": _EOC(r["orden_compra__estatus"]).label,
            "fecha_entrega_estimada": r["orden_compra__fecha_entrega_estimada"],
            "cantidad": Decimal(r["piezas"] or 0), "recibido": r["recibidas"], "pendiente": r["pendientes"],
        }
        for r in renglones_oc(empresa_id, sucursal_id).filter(producto_variante_id=variante_id).order_by()
        .values("orden_compra_id", "orden_compra__folio", "orden_compra__sucursal_id",
                "orden_compra__proveedor__nombre", "orden_compra__estatus", "orden_compra__fecha_entrega_estimada")
        .annotate(piezas=Sum("cantidad"), recibidas=Sum("recibido"), pendientes=Sum("pendiente"))
    ]
    for lista in (entradas_op, entradas_oc):
        lista.sort(key=lambda e: (e["fecha_entrega_estimada"] or date.max, e["folio"] or ""))

    return {
        "totales": _totales(
            sum((e["cantidad"] for e in existencia), CERO),
            sum((c["pendiente"] for c in compromisos), CERO),
            sum((e["pendiente"] for e in entradas_op), CERO),
            sum((e["pendiente"] for e in entradas_oc), CERO),
            [e["fecha_entrega_estimada"] for e in entradas_op + entradas_oc],
        ),
        "existencias": existencia,
        "compromisos": compromisos,
        "entradas_produccion": entradas_op,
        "entradas_compra": entradas_oc,
    }
