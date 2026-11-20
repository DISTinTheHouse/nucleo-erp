"""Recepción de SM (salida a maquila) en WMS y su factura de maquila.

Reglas (#1232, #1234, #1242):

- Las piezas de una SM de OP Enviada quedan **apartadas** en la OP: la
  recepción directa de la OP solo puede recibir lo que no está en maquila, y
  esas piezas solo se reciben con ``salida_maquila``. Así una SM ya no queda
  Enviada para siempre porque la OP se recibió por fuera.
- Contra una SM de OP se recibe a lo más lo pendiente de **la SM** (piezas de
  sus renglones menos lo ya recibido con ella), por ``op_detalle``. Puede ser
  parcial: la SM sigue Enviada hasta completar sus piezas.
- SM de OB/OR: se cuentan las piezas que regresan por renglón de la SM
  (``salida_maquila_detalle``), sin inventario: las prendas ya son del pedido.
  Sin renglones en el POST se recibe todo lo pendiente.
- Cualquier SM queda Recibida al completar sus piezas, o antes si el POST trae
  ``cerrar_salida_maquila``: lo que no regresó queda como faltante
  (``piezas_enviadas − piezas_recibidas``) y no se factura.
- Cada recepción de SM crea su factura de maquila en Borrador **con líneas**:
  una por renglón de la SM, por lo que llegó en esa recepción, al precio de la
  SM (#385: el encabezado se deriva de las líneas).

"Recibido con una SM" = renglones de recepciones activas, no canceladas, ligadas
a la SM; sin restar el rechazo de calidad (lo enviado y devuelto por el
maquilero es lo que se le paga; el rechazo se ajusta en CxP).
"""
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import DecimalField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework.exceptions import ValidationError

from compras.models import Recepcion, RecepcionDetalle
from finanzas.models import FacturaProveedor, FacturaProveedorDetalle
from finanzas.services.factura_proveedor_montos import totales_desde_lineas
from nucleo.models import Moneda
from produccion.models import OrdenProduccion, SalidaMaquila, SalidaMaquilaDetalle

CERO = Decimal("0")
CENTAVO = Decimal("0.01")

# Estatus de OP contra los que se puede recibir (onboarding GET y POST).
ESTATUS_OP_RECIBIBLES = (
    OrdenProduccion.EstatusOrdenProduccion.PENDIENTE,
    OrdenProduccion.EstatusOrdenProduccion.PREPARACION,
    OrdenProduccion.EstatusOrdenProduccion.BORDANDO,
    OrdenProduccion.EstatusOrdenProduccion.REVISION,
)


def origen_sm(sm):
    """``("OB"|"OR"|"OP", folio)`` del documento de origen (relaciones ya cargadas)."""
    if sm.ob_id:
        return "OB", sm.ob.folio_bordado
    if sm.orden_r_id:
        return "OR", sm.orden_r.folio_reflejante
    return "OP", sm.op.folio_op


def _recibido_por_sm(sm_ids):
    """``{(sm_id, op_detalle_id): piezas}`` recibidas con cada SM. Una consulta."""
    filas = (
        RecepcionDetalle.objects.filter(
            recepcion__salida_maquila_id__in=sm_ids, recepcion__activo=True,
            orden_produccion_detalle_id__isnull=False,
        )
        .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
        .values("recepcion__salida_maquila_id", "orden_produccion_detalle_id")
        .annotate(total=Sum("cantidad_recibida"))
    )
    return {(f["recepcion__salida_maquila_id"], f["orden_produccion_detalle_id"]): f["total"] or CERO for f in filas}


def recibido_sm_subquery(sm_ref, op_detalle_ref=None):
    """Piezas recibidas con una SM (y renglón de OP) como subconsulta anotable.
    Sin ``op_detalle_ref``, todas las de la SM: renglones de OP o de la SM (OB/OR)."""
    filtro = {"recepcion__salida_maquila_id": sm_ref}
    if op_detalle_ref is not None:
        filtro["orden_produccion_detalle_id"] = op_detalle_ref
    sub = (
        RecepcionDetalle.objects.filter(recepcion__activo=True, **filtro)
        .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
        .order_by().values("recepcion__salida_maquila_id")
        .annotate(t=Sum("cantidad_recibida")).values("t")
    )
    return Coalesce(Subquery(sub, output_field=DecimalField(max_digits=18, decimal_places=4)),
                    Value(CERO), output_field=DecimalField(max_digits=18, decimal_places=4))


def pendiente_de_sm(sm):
    """``{op_detalle_id: piezas por recibir}`` de una SM de OP. Dos consultas."""
    piezas = defaultdict(lambda: CERO)
    for opd, cantidad in sm.detalles.filter(op_detalle__isnull=False).values_list("op_detalle_id", "cantidad"):
        piezas[opd] += cantidad
    recibido = _recibido_por_sm([sm.pk])
    return {opd: total - recibido.get((sm.pk, opd), CERO) for opd, total in piezas.items()}


def apartado_en_maquila(op_detalle_ids, excluir_sm_id=None):
    """Piezas en maquila (SM de OP Enviadas, aún no recibidas) por renglón de OP:
    ``{op_detalle_id: (piezas, [folios], [{id, folio, cantidad_pendiente}])}``; la
    lista es lo pendiente de cada SM (el tope al recibir con ella). Una consulta,
    sin importar filas."""
    op_detalle_ids = list(op_detalle_ids)
    if not op_detalle_ids:
        return {}
    smd = SalidaMaquilaDetalle.objects.filter(
        op_detalle_id__in=op_detalle_ids, salida__activo=True,
        salida__estatus=SalidaMaquila.Estatus.ENVIADA,
    )
    if excluir_sm_id:
        smd = smd.exclude(salida_id=excluir_sm_id)
    filas = (
        smd.values("salida_id", "salida__folio", "op_detalle_id")
        .annotate(total=Sum("cantidad"))
        .annotate(recibido=recibido_sm_subquery(OuterRef("salida_id"), OuterRef("op_detalle_id")))
        .order_by("salida_id")
    )
    apartado = {}
    for f in filas:
        pend = (f["total"] or CERO) - f["recibido"]
        if pend <= 0:
            continue
        piezas, folios, por_sm = apartado.get(f["op_detalle_id"], (CERO, [], []))
        sm = {"id": f["salida_id"], "folio": f["salida__folio"], "cantidad_pendiente": str(pend.quantize(CENTAVO))}
        apartado[f["op_detalle_id"]] = (piezas + pend, folios + [f["salida__folio"]], por_sm + [sm])
    return apartado


def recibido_por_renglon_sm(sm, detalles):
    """``{salida_maquila_detalle_id: piezas recibidas}`` de una SM. Renglón de
    OB/OR: lo recibido con él. Renglón de OP: lo recibido con la SM en su
    ``op_detalle``, repartido en orden entre los renglones de la SM de ese
    ``op_detalle`` (mismo reparto que la factura). Una consulta."""
    directo = dict(
        RecepcionDetalle.objects.filter(recepcion__salida_maquila_id=sm.pk, recepcion__activo=True,
                                        salida_maquila_detalle__isnull=False)
        .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
        .values("salida_maquila_detalle_id").annotate(t=Sum("cantidad_recibida"))
        .values_list("salida_maquila_detalle_id", "t")
    ) if sm.op_id is None else {}
    por_op = dict(_recibido_por_sm([sm.pk])) if sm.op_id is not None else {}
    resto = {opd: total for (_, opd), total in por_op.items()}
    out = {}
    for d in sorted(detalles, key=lambda x: x.pk):
        if d.op_detalle_id is not None and sm.op_id is not None:
            toma = min(resto.get(d.op_detalle_id, CERO), d.cantidad)
            resto[d.op_detalle_id] = resto.get(d.op_detalle_id, CERO) - toma
            out[d.pk] = toma
        else:
            out[d.pk] = directo.get(d.pk) or CERO
    return out


def renglones_sm_servicio(sm_ids):
    """``{sm_id: [renglón]}`` de SM de OB/OR con SKU, talla, color y lo pendiente
    (enviado − recibido con la SM). Una consulta, sin importar SM ni renglones."""
    from catalogo.models import ProductoVariante

    sm_ids = list(sm_ids)
    if not sm_ids:
        return {}
    recibido = (
        RecepcionDetalle.objects.filter(salida_maquila_detalle_id=OuterRef("pk"), recepcion__activo=True)
        .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
        .order_by().values("salida_maquila_detalle_id").annotate(t=Sum("cantidad_recibida")).values("t")
    )
    sku = ProductoVariante.objects.filter(
        producto_id=OuterRef("_producto"), talla_id=OuterRef("_talla"), color_id=OuterRef("_color"),
    ).order_by("id").values("sku")[:1]
    filas = (
        SalidaMaquilaDetalle.objects.filter(salida_id__in=sm_ids)
        .annotate(
            _producto=Coalesce("ob_detalle__producto_id", "or_detalle__producto_id"),
            _talla=Coalesce("ob_detalle__talla_id", "or_detalle__talla_id"),
            _color=Coalesce("ob_detalle__color_id", "or_detalle__color_id"),
        )
        .annotate(
            producto_nombre=Coalesce("ob_detalle__producto__nombre", "or_detalle__producto__nombre"),
            producto_codigo=Coalesce("ob_detalle__producto__codigo", "or_detalle__producto__codigo"),
            producto_cod_proscai=Coalesce("ob_detalle__producto__cod_proscai", "or_detalle__producto__cod_proscai"),
            talla=Coalesce("ob_detalle__talla__nombre", "or_detalle__talla__nombre"),
            color=Coalesce("ob_detalle__color__nombre", "or_detalle__color__nombre"),
            sku=Subquery(sku),
            recibido=Coalesce(Subquery(recibido, output_field=DecimalField(max_digits=18, decimal_places=4)),
                              Value(CERO), output_field=DecimalField(max_digits=18, decimal_places=4)),
        )
        .order_by("salida_id", "id")
        .values("id", "salida_id", "ob_detalle_id", "or_detalle_id", "descripcion", "producto_nombre",
                "sku", "talla", "color", "cantidad", "recibido", "_producto", "producto_codigo", "producto_cod_proscai")
    )
    out = {}
    for f in filas:
        out.setdefault(f["salida_id"], []).append({
            "id": f["id"], "ob_detalle": f["ob_detalle_id"], "or_detalle": f["or_detalle_id"],
            "producto_id": f["_producto"], "producto_nombre": f["producto_nombre"], "descripcion": f["descripcion"],
            "sku": f["sku"], "talla": f["talla"], "color": f["color"],
            "producto_codigo": f["producto_codigo"], "producto_cod_proscai": f["producto_cod_proscai"],
            "cantidad": str(f["cantidad"].quantize(CENTAVO)),
            "cantidad_recibida": str(f["recibido"].quantize(CENTAVO)),
            "cantidad_pendiente": str(max(f["cantidad"] - f["recibido"], CERO).quantize(CENTAVO)),
        })
    return out


def _moneda_mxn():
    moneda = Moneda.objects.filter(codigo_iso="MXN").order_by("pk").first()
    if moneda is None:
        raise ValidationError({"moneda": ["No existe la moneda MXN para la factura de maquila."]})
    return moneda


def _linea(smd, cantidad, recepcion_detalle=None, producto=None):
    subtotal = (cantidad * smd.precio_unitario).quantize(CENTAVO, ROUND_HALF_UP)
    return FacturaProveedorDetalle(
        salida_maquila_detalle=smd, recepcion_detalle=recepcion_detalle, producto=producto,
        descripcion=(smd.descripcion or "")[:255], cantidad=cantidad,
        precio_unitario=smd.precio_unitario.quantize(CENTAVO, ROUND_HALF_UP),
        descuento=CERO, impuesto=CERO, subtotal=subtotal, total=subtotal,
    )


def facturar_maquila(sm, recepcion, renglones_recepcion=None):
    """Factura de maquila en Borrador para lo que llegó en ``recepcion``.

    ``renglones_recepcion``: los ``RecepcionDetalle`` creados. Uno de SM de OB/OR
    va a su ``salida_maquila_detalle``; uno de OP se reparte entre los renglones
    de la SM de su ``op_detalle`` en orden. Sin ellos se factura la SM completa."""
    detalles_sm = list(sm.detalles.order_by("id"))
    lineas = []
    if renglones_recepcion is None:
        lineas = [_linea(d, d.cantidad) for d in detalles_sm if d.cantidad > 0]
    else:
        # Lo ya facturado por renglón de SM en recepciones anteriores.
        facturado = dict(
            FacturaProveedorDetalle.objects.filter(
                salida_maquila_detalle__salida=sm, factura_proveedor__activo=True,
            )
            .exclude(factura_proveedor__estatus=FacturaProveedor.FacturaProveedorStatus.CANCELADA)
            .values("salida_maquila_detalle_id").annotate(t=Sum("cantidad")).values_list("salida_maquila_detalle_id", "t")
        )
        libre = {d.pk: d.cantidad - (facturado.get(d.pk) or CERO) for d in detalles_sm}
        for rd in renglones_recepcion:
            resto = rd.cantidad_recibida
            for d in detalles_sm:
                if resto <= 0:
                    break
                if rd.salida_maquila_detalle_id:
                    if d.pk != rd.salida_maquila_detalle_id:
                        continue
                elif d.op_detalle_id != rd.orden_produccion_detalle_id:
                    continue
                if libre[d.pk] <= 0:
                    continue
                toma = min(resto, libre[d.pk])
                libre[d.pk] -= toma
                resto -= toma
                lineas.append(_linea(d, toma, rd, rd.producto))  # muestra: sin producto
    if not lineas:
        return None
    origen, folio_origen = origen_sm(sm)
    piezas = sum((l.cantidad for l in lineas), CERO)
    factura = FacturaProveedor.objects.create(
        empresa_id=sm.empresa_id, sucursal_id=sm.sucursal_id, proveedor_id=sm.centro_confeccion_id,
        oc=None, recepcion=recepcion, salida_maquila=sm, moneda=_moneda_mxn(),
        estatus=FacturaProveedor.FacturaProveedorStatus.BORRADOR,
        observaciones=f"Maquila {sm.folio} ({origen} {folio_origen}), {piezas.normalize():f} pzas.",
        **totales_desde_lineas(lineas),
    )
    for linea in lineas:
        linea.factura_proveedor = factura
    FacturaProveedorDetalle.objects.bulk_create(lineas)
    return factura


def marcar_recibida(sm):
    from django.utils import timezone

    sm.estatus = SalidaMaquila.Estatus.RECIBIDA
    sm.fecha_recepcion = timezone.now()
    sm.save(update_fields=["estatus", "fecha_recepcion", "updated_at"])
