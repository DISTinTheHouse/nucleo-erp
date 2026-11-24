"""Devolución a proveedor de lo que calidad rechazó o puso en cuarentena.

Lo rechazado/en cuarentena no entra a ``Existencia`` (calidad solo abona lo
aprobado). Por eso la SALIDA de inventario es de delta 0: queda el movimiento
formal y costeado (``costeo.costear`` con delta 0 = al promedio vigente) sin
descontar un stock que nunca entró. El ``total`` del documento es lo que se
reclama al proveedor: cantidad × costo unitario del renglón de la OC.

Consultas fijas por documento, no por renglón.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from auditoria.models import AuditoriaEvento
from compras.models import (
    CalidadInspeccion, CalidadInspeccionDetalle, DevolucionProveedor, DevolucionProveedorDetalle, OrdenCompraDetalle,
)
from inventarios.models import MovimientoInventario, MovimientoInventarioDetalle
from inventarios.services import costeo
from nucleo.models import SerieFolio

RESULTADOS_DEVOLVIBLES = ("rechazo", "cuarentena")
TIPO_DOCUMENTO_FOLIO = "DEVOLUCION_PROVEEDOR"
CERO = Decimal("0")
CENT = Decimal("0.01")


def _folio(empresa_id, sucursal_id):
    """Serie ``DEVOLUCION_PROVEEDOR`` de la sucursal si existe; si no, ``DVP-AAMMDDhhmmssµs``."""
    serie = SerieFolio.resolve(empresa_id, sucursal_id, TIPO_DOCUMENTO_FOLIO, lock=True)
    if serie is not None:
        try:
            folio, consecutivo, anio = serie.get_siguiente_folio()
        except ValueError:
            raise ValidationError({"folio": f"Serie {serie.serie}: no fue posible generar el folio."})
        serie.folio_actual, serie.ultimo_anio = consecutivo, anio
        serie.save(update_fields=["folio_actual", "ultimo_anio"])
        return folio
    return f"DVP-{timezone.now():%y%m%d%H%M%S%f}"


def _costo_oc(detalles_recepcion):
    """Costo unitario en MXN por renglón de recepción, del renglón de su OC (1 consulta)."""
    ids = {d.orden_compra_detalle_id for d in detalles_recepcion if d.orden_compra_detalle_id}
    lineas = {
        l.pk: l for l in OrdenCompraDetalle.objects.filter(pk__in=ids).select_related("orden_compra__moneda")
    }
    costos = {}
    for d in detalles_recepcion:
        linea = lineas.get(d.orden_compra_detalle_id)
        if linea is None:
            continue
        unitario = (linea.importe / linea.cantidad) if linea.cantidad else linea.precio
        oc = linea.orden_compra
        if (oc.moneda.codigo_iso or "").upper() != "MXN":
            unitario = unitario * oc.tipo_cambio if oc.tipo_cambio else CERO
        costos[d.pk] = Decimal(str(unitario or 0))
    return costos


def pendientes(inspeccion_id):
    """``{calidad_detalle_id: cantidad aún devolvible}`` (2 consultas)."""
    filas = list(
        CalidadInspeccionDetalle.objects.filter(calidad_inspeccion_id=inspeccion_id, resultado__in=RESULTADOS_DEVOLVIBLES)
        .select_related("recepcion_detalle")
    )
    devuelto = dict(
        DevolucionProveedorDetalle.objects.filter(
            calidad_detalle__in=[f.pk for f in filas], devolucion__estatus=DevolucionProveedor.Estatus.APLICADA,
        ).values_list("calidad_detalle_id").annotate(t=Sum("cantidad"))
    )
    salida = {}
    for f in filas:
        base = f.cantidad_rechazada if f.cantidad_rechazada > 0 else (f.cantidad_inspeccionada - f.cantidad_aprobada)
        salida[f.pk] = (f, max(Decimal(str(base)) - (devuelto.get(f.pk) or CERO), CERO))
    return salida


def _movimiento(dev, tipo, renglones, observaciones, user):
    """Movimiento de delta 0 ligado a la recepción y costeado (``costeo``)."""
    claves = {costeo.clave(d.producto_id, d.producto_variante_id, d.variante_produccion_id) for d in renglones}
    existencia = defaultdict(lambda: CERO, costeo._existencias(dev.empresa_id, claves))
    mov = MovimientoInventario.objects.create(
        empresa_id=dev.empresa_id, sucursal_id=dev.sucursal_id, tipo_movimiento=tipo, usuario=user,
        observaciones=observaciones[:150], recepcion_id=dev.recepcion_id,
    )
    detalles = MovimientoInventarioDetalle.objects.bulk_create([
        MovimientoInventarioDetalle(
            movimiento_inventario=mov, producto_id=d.producto_id, producto_variante_id=d.producto_variante_id,
            variante_produccion_id=d.variante_produccion_id, cantidad=d.cantidad, costo_unitario=CERO,
            cantidad_antes=existencia[costeo.clave(d.producto_id, d.producto_variante_id, d.variante_produccion_id)],
            cantidad_despues=existencia[costeo.clave(d.producto_id, d.producto_variante_id, d.variante_produccion_id)],
        )
        for d in renglones
    ])
    costeo.costear(dev.empresa_id, [(m, 0, None) for m in detalles], costeo.Origen.OPERACION)
    return mov


@transaction.atomic
def crear(*, inspeccion, motivo, renglones, user, ip=None, user_agent=None):
    """``renglones``: ``[{"calidad_detalle": id, "cantidad": x}]``; vacío = todo lo pendiente."""
    inspeccion = (
        CalidadInspeccion.objects.select_for_update(of=("self",))
        .select_related("recepcion__orden_compra").get(pk=inspeccion.pk)
    )
    if inspeccion.estado == CalidadInspeccion.ESTADO_ANULADA:
        raise ValidationError({"calidad_inspeccion": "La inspección está anulada."})
    recepcion = inspeccion.recepcion
    disponibles = pendientes(inspeccion.pk)
    if not renglones:
        renglones = [{"calidad_detalle": k, "cantidad": v} for k, (_, v) in disponibles.items() if v > 0]
    if not renglones:
        raise ValidationError({"detalles": "No hay nada rechazado ni en cuarentena pendiente de devolver."})
    pedidos = defaultdict(lambda: CERO)
    for r in renglones:
        try:
            pedidos[int(r["calidad_detalle"])] += Decimal(str(r["cantidad"]))
        except Exception:
            raise ValidationError({"detalles": "Cada renglón lleva calidad_detalle y cantidad."})
    for cd_id, cantidad in pedidos.items():
        if cd_id not in disponibles:
            raise ValidationError({"detalles": f"El renglón {cd_id} no es rechazo ni cuarentena de esta inspección."})
        if cantidad <= 0 or cantidad > disponibles[cd_id][1]:
            raise ValidationError({"detalles": (
                f"Renglón {cd_id}: se pueden devolver hasta {disponibles[cd_id][1].normalize():f}.")})

    filas = {k: disponibles[k][0] for k in pedidos}
    costos = _costo_oc([f.recepcion_detalle for f in filas.values()])
    dev = DevolucionProveedor.objects.create(
        empresa_id=recepcion.empresa_id, sucursal_id=recepcion.sucursal_id,
        folio=_folio(recepcion.empresa_id, recepcion.sucursal_id),
        calidad_inspeccion=inspeccion, recepcion=recepcion, orden_compra_id=recepcion.orden_compra_id,
        proveedor_id=recepcion.proveedor_id or getattr(recepcion.orden_compra, "proveedor_id", None),
        almacen_id=recepcion.almacen_id, motivo=motivo, usuario=user,
    )
    detalles = []
    for cd_id, cantidad in pedidos.items():
        rd = filas[cd_id].recepcion_detalle
        unitario = costos.get(rd.pk, CERO)
        detalles.append(DevolucionProveedorDetalle(
            devolucion=dev, calidad_detalle_id=cd_id, recepcion_detalle=rd, producto_id=rd.producto_id,
            producto_variante_id=rd.producto_variante_id, variante_produccion_id=rd.variante_produccion_id,
            cantidad=cantidad, costo_unitario=unitario, importe=(cantidad * unitario).quantize(CENT),
        ))
    DevolucionProveedorDetalle.objects.bulk_create(detalles)
    dev.movimiento_inventario = _movimiento(dev, "SALIDA", detalles, f"Devolución a proveedor {dev.folio}: {motivo}", user)
    dev.total = sum((d.importe for d in detalles), CERO)
    dev.save(update_fields=["movimiento_inventario", "total", "updated_at"])
    AuditoriaEvento.objects.create(
        empresa_id=dev.empresa_id, usuario=user, modulo="compras", accion="CREAR",
        tabla=DevolucionProveedor._meta.db_table, id_registro=str(dev.pk), ip=ip, user_agent=user_agent,
        antes_json=None,
        despues_json={
            "folio": dev.folio, "estatus": dev.estatus, "motivo": motivo, "total": str(dev.total),
            "calidad_inspeccion_id": inspeccion.pk, "recepcion_id": recepcion.pk,
            "orden_compra_id": dev.orden_compra_id, "movimiento_inventario_id": dev.movimiento_inventario_id,
            "detalles": [{"calidad_detalle_id": d.calidad_detalle_id, "cantidad": str(d.cantidad),
                          "costo_unitario": str(d.costo_unitario)} for d in detalles],
        },
    )
    return dev


@transaction.atomic
def cancelar(*, devolucion, motivo, user, ip=None, user_agent=None):
    """Cancela la devolución: ENTRADA de delta 0 que revierte la salida, con motivo."""
    dev = DevolucionProveedor.objects.select_for_update().get(pk=devolucion.pk)
    if dev.estatus == DevolucionProveedor.Estatus.CANCELADA:
        raise ValidationError({"estatus": "La devolución ya está cancelada."})
    detalles = list(dev.detalles.all())
    mov = _movimiento(dev, "ENTRADA", detalles, f"Cancelación de devolución {dev.folio}: {motivo}", user)
    dev.estatus = DevolucionProveedor.Estatus.CANCELADA
    dev.motivo_cancelacion, dev.fecha_cancelacion, dev.cancelado_por = motivo, timezone.now(), user
    dev.save(update_fields=["estatus", "motivo_cancelacion", "fecha_cancelacion", "cancelado_por", "updated_at"])
    AuditoriaEvento.objects.create(
        empresa_id=dev.empresa_id, usuario=user, modulo="compras", accion="CANCELAR",
        tabla=DevolucionProveedor._meta.db_table, id_registro=str(dev.pk), ip=ip, user_agent=user_agent,
        antes_json={"estatus": DevolucionProveedor.Estatus.APLICADA},
        despues_json={"estatus": dev.estatus, "motivo": motivo, "movimiento_inventario_id": mov.pk},
    )
    return dev
