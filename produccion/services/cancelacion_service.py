"""Cancelación de órdenes de producción con rastro completo.

- OP: al cancelarla se devuelve al almacén lo que consumió (neto de recálculos),
  al mismo costo con que salió, y se bloquea si tiene recepciones o SM vivas.
- OB/OR/OCM: ``cancelar/`` con motivo; no mueven inventario.

El motivo, quién y cuándo quedan en la orden (``motivo_cancelacion``,
``cancelado_por``, ``fecha_cancelacion``), en ``AuditoriaEvento`` y, por la
señal del cambio de estatus, en la bitácora de documentos.
"""
from collections import defaultdict
from nucleo.motivo import exigir_largo_motivo
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from auditoria.models import AuditoriaEvento
from inventarios.models import Existencia, MovimientoInventario, MovimientoInventarioDetalle, TipoMovimiento
from inventarios.services import costeo
from nucleo.middleware import ip_auditoria
from produccion.models import (
    ConsumoProduccion, ConsumoProduccionDetalle, OrdenesBordado, OrdenesCorteManga, OrdenesReflejante,
    SalidaMaquila,
)

CERO = Decimal("0")


def motivo_de(data):
    motivo = data.get("motivo") if hasattr(data, "get") else None
    if not isinstance(motivo, str) or not motivo.strip():
        raise ValidationError({"motivo": "Indica el motivo de la cancelación."})
    return exigir_largo_motivo(motivo.strip())


def _meta(request):
    if request is None:
        return None, None
    return ip_auditoria(request), request.META.get("HTTP_USER_AGENT")


def _usuario(user):
    return user if getattr(user, "pk", None) else None


def sellar(orden, user, motivo):
    """Campos de cancelación de la orden (los guarda quien llama)."""
    orden.motivo_cancelacion = motivo
    orden.fecha_cancelacion = timezone.now()
    orden.cancelado_por = _usuario(user)
    return ["motivo_cancelacion", "fecha_cancelacion", "cancelado_por"]


# --------------------------------------------------------------------- OP

def exigir_op_cancelable(op):
    """Lo que cuelga de la OP y ya existe en el mundo bloquea: primero se cancela."""
    from compras.models import Recepcion

    rec = (
        Recepcion.objects.filter(op=op, activo=True)
        .exclude(estatus=Recepcion.EstatusRecepcion.CANCELADA)
        .values_list("folio", flat=True).first()
    )
    if rec is not None:
        raise ValidationError({"recepciones": (
            f"La OP tiene la recepción {rec or ''} de producto terminado: cancélala primero en WMS."
        ).replace("  ", " ")})
    sm = SalidaMaquila.objects.filter(op=op, estatus__in=(SalidaMaquila.Estatus.ENVIADA, SalidaMaquila.Estatus.RECIBIDA)).values_list("folio", "estatus").first()
    if sm is not None:
        raise ValidationError({"salidas_maquila": f"La OP tiene la salida a maquila {sm[0]} {'recibida' if sm[1] == SalidaMaquila.Estatus.RECIBIDA else 'enviada'}: cancélala primero."})


def devolver_consumo_op(op, user, motivo, request=None):
    """ENTRADA que regresa lo consumido neto por la OP (consumo inicial menos lo
    devuelto en recálculos) a la misma existencia (almacén/ubicación), al costo
    promedio con que salió. Deja ``ConsumoProduccion`` negativo para que el
    consumo de la OP quede en cero y el evento de inventario para los reportes.
    Regresa los renglones devueltos."""
    detalles = (
        MovimientoInventarioDetalle.objects.filter(
            movimiento_inventario__op=op,
            movimiento_inventario__recepcion__isnull=True,
            movimiento_inventario__transferencia__isnull=True,
            movimiento_inventario__tipo_movimiento__in=[TipoMovimiento.SALIDA, TipoMovimiento.ENTRADA],
        )
        .values(
            "movimiento_inventario__tipo_movimiento", "producto_id", "producto_variante_id",
            "variante_produccion_id", "almacen_id", "ubicacion_origen_id", "ubicacion_destino_id",
            "cantidad", "costo_unitario",
        )
    )
    neto = defaultdict(lambda: [CERO, CERO, CERO])  # cantidad neta, Σ costo×salida, Σ salida
    for d in detalles:
        salida = d["movimiento_inventario__tipo_movimiento"] == TipoMovimiento.SALIDA
        ubic = d["ubicacion_origen_id"] if salida else d["ubicacion_destino_id"]
        llave = (d["producto_id"], d["producto_variante_id"], d["variante_produccion_id"], d["almacen_id"], ubic)
        cant = Decimal(str(d["cantidad"] or 0))
        fila = neto[llave]
        if salida:
            fila[0] += cant
            fila[1] += cant * Decimal(str(d["costo_unitario"] or 0))
            fila[2] += cant
        else:
            fila[0] -= cant
    renglones = [(k, v) for k, v in neto.items() if v[0] > 0 and k[3]]
    if not renglones:
        return []

    entradas = []
    for (prod, var, vp, alm, ubic), (cant, importe, salidas) in sorted(renglones, key=lambda r: tuple(x or 0 for x in r[0])):
        ex = (
            Existencia.objects.select_for_update()
            .filter(producto_id=prod, producto_variante_id=var, variante_produccion_id=vp,
                    almacen_id=alm, ubicacion_id=ubic)
            .order_by("id").first()
        )
        if ex is None:
            ex = Existencia(producto_id=prod, producto_variante_id=var, variante_produccion_id=vp,
                            almacen_id=alm, ubicacion_id=ubic, stock=0, cantidad=CERO)
        antes = Decimal(str(ex.cantidad or 0))
        ex.cantidad = antes + cant
        ex.stock = max(int(ex.cantidad), 0)
        ex.save()
        costo = (importe / salidas) if salidas else None
        entradas.append((MovimientoInventarioDetalle(
            producto_id=prod, producto_variante_id=var, variante_produccion_id=vp, almacen_id=alm,
            ubicacion_destino_id=ubic, cantidad=cant, costo_unitario=CERO,
            cantidad_antes=antes, cantidad_despues=ex.cantidad,
        ), costo))

    mov = MovimientoInventario.objects.create(
        empresa_id=op.empresa_id, sucursal_id=op.sucursal_id, pedido_id=op.pedido_id, op=op,
        tipo_movimiento=TipoMovimiento.ENTRADA, usuario=_usuario(user),
        observaciones=f"Devolución de consumo por cancelación de OP {op.folio_op}: {motivo}"[:150],
    )
    for d, _ in entradas:
        d.movimiento_inventario = mov
    MovimientoInventarioDetalle.objects.bulk_create([d for d, _ in entradas])
    costeo.costear(op.empresa_id, [(d, d.cantidad, c) for d, c in entradas], costeo.Origen.CONSUMO_OP)

    por_producto = defaultdict(lambda: CERO)
    for d, _ in entradas:
        por_producto[d.producto_id] -= d.cantidad
    consumo = ConsumoProduccion.objects.create(op=op)
    ConsumoProduccionDetalle.objects.bulk_create([
        ConsumoProduccionDetalle(consumo_produccion=consumo, producto_id=p, cantidad=c)
        for p, c in por_producto.items()
    ])

    items = [
        {
            "producto_id": d.producto_id, "producto_variante_id": d.producto_variante_id,
            "variante_produccion_id": d.variante_produccion_id, "almacen_id": d.almacen_id,
            "ubicacion_id": d.ubicacion_destino_id, "cantidad_before": str(d.cantidad_antes),
            "cantidad_after": str(d.cantidad_despues), "delta": str(d.cantidad),
        }
        for d, _ in entradas
    ]
    ip, ua = _meta(request)
    AuditoriaEvento.objects.create(
        empresa_id=op.empresa_id, usuario=_usuario(user), modulo="inventarios", accion="ENTRADA",
        tabla="existencias", id_registro=str(op.op_id),
        antes_json={"items": items, "op_id": op.op_id, "folio_op": op.folio_op},
        despues_json={
            "empresa_id": op.empresa_id, "sucursal_id": op.sucursal_id, "op_id": op.op_id,
            "folio_op": op.folio_op, "cancelacion_op": True, "motivo": motivo,
            "movimiento_inventario_id": mov.pk, "items": items,
        },
        ip=ip, user_agent=ua,
    )
    return items


# ------------------------------------------------------------ OB / OR / OCM

ORDENES_SERVICIO = {
    "bordado": (OrdenesBordado, "estatus_bordado", OrdenesBordado.EstatusBordado.CANCELADO_LEGACY,
                {OrdenesBordado.EstatusBordado.FINALIZADO}, "folio_bordado", "D-PRODUCCION-OB", "ob"),
    "reflejante": (OrdenesReflejante, "estatus_reflejante", OrdenesReflejante.EstatusReflejante.CANCELADO,
                   {OrdenesReflejante.EstatusReflejante.COMPLETADO}, "folio_reflejante", "D-PRODUCCION-OR", "orden_r"),
    "corte_manga": (OrdenesCorteManga, "estatus_corte", OrdenesCorteManga.EstatusCorte.CANCELADO,
                    {OrdenesCorteManga.EstatusCorte.COMPLETADO}, "folio_ocm", "D-PRODUCCION-CM", None),
}


def estatus_cancelado(tipo):
    return ORDENES_SERVICIO[tipo][2]


@transaction.atomic
def cancelar_orden_servicio(tipo, orden, user, motivo, request=None):
    modelo, campo, cancelado, cerrados, folio, permiso, fk_sm = ORDENES_SERVICIO[tipo]
    if not user.tiene_permiso(permiso):
        raise PermissionDenied(f"No tienes permiso para cancelar ({permiso}).")
    orden = modelo.objects.select_for_update().get(pk=orden.pk)
    actual = getattr(orden, campo)
    if actual == cancelado:
        raise ValidationError({campo: "La orden ya está cancelada."})
    if not orden.activo:
        raise ValidationError({campo: "La orden está dada de baja."})
    if actual in cerrados:
        raise ValidationError({campo: "La orden ya está terminada; no se cancela."})
    if fk_sm:
        sm = SalidaMaquila.objects.filter(**{fk_sm: orden}, estatus__in=(
            SalidaMaquila.Estatus.ENVIADA, SalidaMaquila.Estatus.RECIBIDA)).values_list("folio", "estatus").first()
        if sm is not None:
            raise ValidationError({"salidas_maquila": f"La orden tiene la salida a maquila {sm[0]} {'recibida' if sm[1] == SalidaMaquila.Estatus.RECIBIDA else 'enviada'}: cancélala primero."})
    setattr(orden, campo, cancelado)
    orden.save(update_fields=[campo, *sellar(orden, user, motivo)])
    ip, ua = _meta(request)
    AuditoriaEvento.objects.create(
        empresa_id=orden.empresa_id, usuario=_usuario(user), modulo="produccion", accion="CANCELAR",
        tabla=modelo._meta.db_table, id_registro=str(orden.pk),
        antes_json={campo: int(actual), "folio": getattr(orden, folio)},
        despues_json={campo: int(cancelado), "folio": getattr(orden, folio), "motivo": motivo},
        ip=ip, user_agent=ua,
    )
    return orden


def auditar_baja(request, modelo_tabla, empresa_id, registro_id, antes):
    """DELETE (baja lógica) de órdenes y avances: quién, cuándo y qué."""
    ip, ua = _meta(request)
    AuditoriaEvento.objects.create(
        empresa_id=empresa_id, usuario=_usuario(request.user), modulo="produccion", accion="BAJA",
        tabla=modelo_tabla, id_registro=str(registro_id), antes_json=antes,
        despues_json={"activo": False, "motivo": (request.data.get("motivo") if hasattr(request.data, "get") else None)
                      or request.query_params.get("motivo") or None},
        ip=ip, user_agent=ua,
    )
