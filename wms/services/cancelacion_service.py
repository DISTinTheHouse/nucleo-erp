"""Cancelación de documentos de WMS: picking, packing, despacho y reversa de TR.

Reglas comunes:

- Cancelar no borra. El documento conserva su folio, queda en estado cancelado y
  guarda motivo, fecha y usuario (``CancelacionMixin``), más un ``AuditoriaEvento``.
- Se cancela de atrás hacia adelante: un picking con packing vivo no se cancela,
  un packing con despacho activo tampoco. Así cada paso revierte sólo lo suyo.
- Lo que tocó el kardex no se edita: se revierte con un movimiento contrario
  (ENTRADA de reversa del despacho, TR inversa de una transferencia).
- Mismo orden de bloqueo que el alta (EC-574): Pedido -> Picking -> Packing -> Despacho.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from auditoria.models import AuditoriaEvento
from inventarios.models import (
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    TipoMovimiento,
    inventario_reservas,
)
from inventarios.services import costeo
from nucleo.middleware import ip_auditoria
from produccion.models import OrdenesBordado, OrdenesCorteManga, OrdenesReflejante
from ventas.models import Pedido
from wms.models import Despacho, Estado, Packing, Picking, PickingDetalle, Transferencia
from wms.services.existencia_service import ExistenciaService

PERMISO_CANCELAR = "WMS-CANCELAR"


def exigir_permiso(user):
    """Cancelar deshace trabajo de almacén: admin de empresa, superuser, departamento de
    mesa de control o el permiso ``WMS-CANCELAR`` (por rol o como GRANT individual)."""
    if user.is_superuser or user.is_admin_empresa or user.tiene_permiso(PERMISO_CANCELAR):
        return
    # Mesa de control dirige el flujo del pedido: puede deshacer el trabajo de almacén.
    # Misma regla que las acciones de mesa de control de ventas (EC-944).
    from seguridad.role_identity import es_mesa_control
    if es_mesa_control(user):
        return
    raise PermissionDenied("No tienes permiso para cancelar documentos de almacén.")


def motivo_de(data):
    motivo = str(data.get("motivo") or "").strip()[:500] if hasattr(data, "get") else ""
    if not motivo:
        raise ValidationError({"motivo": "Indica el motivo de la cancelación."})
    return motivo


def _marcar(doc, campo_estado, estado, user, motivo):
    setattr(doc, campo_estado, estado)
    doc.motivo_cancelacion = motivo
    doc.fecha_cancelacion = timezone.now()
    doc.cancelado_por = user
    doc.save(update_fields=[campo_estado, "motivo_cancelacion", "fecha_cancelacion", "cancelado_por"])


def _auditar(request, empresa_id, tabla, doc, antes, despues):
    meta = getattr(request, "META", None) or {}
    AuditoriaEvento.objects.create(
        empresa_id=empresa_id, usuario=request.user, modulo="wms", accion="CANCELAR",
        tabla=tabla, id_registro=str(doc.pk), antes_json=antes, despues_json=despues,
        ip=ip_auditoria(request) if request is not None else None,
        user_agent=meta.get("HTTP_USER_AGENT") or None,
    )


def _bloquear_pedido(pedido_id):
    Pedido.objects.select_for_update(of=("self",)).filter(pk=pedido_id).first()


def _ordenes_trabajo_vivas(picking):
    """Folios de órdenes de bordado/reflejante/corte de manga vivas que nacieron del
    picking: cancelarlo las dejaría trabajando prendas de un surtido que ya no existe."""
    filtro = {"picking_ordenes_trabajo__picking": picking, "activo": True}
    consultas = (
        (OrdenesBordado.objects.filter(**filtro)
         .exclude(estatus_bordado=OrdenesBordado.EstatusBordado.CANCELADO_LEGACY), "folio_bordado"),
        (OrdenesReflejante.objects.filter(**filtro)
         .exclude(estatus_reflejante=OrdenesReflejante.EstatusReflejante.CANCELADO), "folio_reflejante"),
        (OrdenesCorteManga.objects.filter(**filtro)
         .exclude(estatus_corte=OrdenesCorteManga.EstatusCorte.CANCELADO), "folio_ocm"),
    )
    folios = []
    for qs, campo in consultas:
        folios += [f or "(sin folio)" for f in qs.values_list(campo, flat=True).distinct()]
    return folios


@transaction.atomic
def cancelar_picking(picking, request, motivo):
    """Picking sin packing vivo ni órdenes de trabajo vivas. No mueve inventario (el
    surtido no lo toca): sus líneas quedan canceladas, el pedido vuelve a tener esas
    piezas por surtir y se liberan sus reservas activas. Si el picking ya se trasladó
    con una TR, las prendas se quedan donde están; para regresarlas, revertir la TR."""
    _bloquear_pedido(picking.pedido_id)
    picking = Picking.objects.select_for_update(of=("self",)).get(pk=picking.pk)
    if picking.estado == Picking.Estado.CANCELADO:
        raise ValidationError({"picking": "El picking ya está cancelado."})
    packings = list(picking.packings.exclude(estado=Estado.CANCELADO).values_list("folio", flat=True))
    if packings:
        raise ValidationError({"picking": f"Tiene packing vivo: {', '.join(packings)}. Cancélalo primero."})
    ordenes = _ordenes_trabajo_vivas(picking)
    if ordenes:
        raise ValidationError({"picking": f"Tiene órdenes de trabajo vivas: {', '.join(ordenes)}. Cancélalas primero."})

    estado_anterior = picking.estado
    lineas = picking.picking_detalle.exclude(estado=PickingDetalle.EstadoLinea.CANCELADA).update(
        estado=PickingDetalle.EstadoLinea.CANCELADA
    )
    reservas = inventario_reservas.objects.filter(
        picking=picking, estado=inventario_reservas.Estado.ACTIVA
    ).update(estado=inventario_reservas.Estado.LIBERADA, fecha_devolucion=timezone.now())
    _marcar(picking, "estado", Picking.Estado.CANCELADO, request.user, motivo)
    _auditar(request, picking.empresa_id, "picking", picking,
             {"folio": picking.folio, "estado": estado_anterior},
             {"folio": picking.folio, "estado": picking.estado, "motivo": motivo,
              "lineas_canceladas": lineas, "reservas_liberadas": reservas})
    return picking


@transaction.atomic
def cancelar_packing(packing, request, motivo):
    """Packing sin despacho activo. No mueve inventario: el picking vuelve a quedar por
    empacar y sus piezas se pueden empacar de nuevo."""
    _bloquear_pedido(packing.pedido_id)
    packing = Packing.objects.select_for_update(of=("self",)).get(pk=packing.pk)
    if packing.estado == Estado.CANCELADO:
        raise ValidationError({"packing": "El packing ya está cancelado."})
    despachos = list(packing.despachos.filter(estado=Despacho.Estado.ACTIVO).values_list("pk", flat=True))
    if despachos:
        raise ValidationError({"packing": f"Tiene despachos activos: {', '.join(map(str, despachos))}. Cancélalos primero."})

    estado_anterior = packing.estado
    lineas = packing.packing_detalle.exclude(estado=Estado.CANCELADO).update(estado=Estado.CANCELADO)
    _marcar(packing, "estado", Estado.CANCELADO, request.user, motivo)
    _auditar(request, packing.empresa_id, "packing", packing,
             {"folio": packing.folio, "estado": estado_anterior},
             {"folio": packing.folio, "estado": packing.estado, "motivo": motivo, "lineas_canceladas": lineas})
    return packing


@transaction.atomic
def cancelar_despacho(despacho, request, motivo):
    """Las prendas regresan al almacén del que salieron: una ENTRADA ligada al mismo
    despacho que espeja su SALIDA renglón por renglón, al mismo costo. El packing
    vuelve a quedar por despachar. Para mercancía ya entregada al cliente no se
    cancela el despacho: eso es una devolución."""
    packing = despacho.packing
    _bloquear_pedido(packing.pedido_id)
    Packing.objects.select_for_update(of=("self",)).filter(pk=packing.pk).first()
    despacho = Despacho.objects.select_for_update(of=("self",)).get(pk=despacho.pk)
    if despacho.estado == Despacho.Estado.CANCELADO:
        raise ValidationError({"despacho": "El despacho ya está cancelado."})

    salida = (
        MovimientoInventario.objects.filter(
            despacho=despacho, tipo_movimiento=TipoMovimiento.SALIDA, activo=True
        ).order_by("pk").first()
    )
    reintegro = _reintegrar_salida(despacho, salida, request.user) if salida else []
    _marcar(despacho, "estado", Despacho.Estado.CANCELADO, request.user, motivo)
    _auditar(request, packing.empresa_id, "despachos", despacho,
             {"estado": Despacho.Estado.ACTIVO, "packing": packing.folio},
             {"estado": despacho.estado, "packing": packing.folio, "motivo": motivo,
              "salida_revertida": getattr(salida, "pk", None), "inventario_devuelto": reintegro})
    return despacho


def _reintegrar_salida(despacho, salida, user):
    """Suma a la existencia del almacén lo que la salida descontó (fila de menor pk de
    la clave, o una nueva) y registra la ENTRADA con antes/después y costo."""
    detalles_salida = list(
        MovimientoInventarioDetalle.objects.filter(movimiento_inventario=salida).order_by("pk")
    )
    filas, nuevas = {}, []
    entradas = []
    for d in detalles_salida:
        llave = (d.almacen_id, d.producto_id, d.producto_variante_id, d.variante_produccion_id)
        if llave not in filas:
            fila = ExistenciaService.get_existencia(
                d.almacen_id, d.producto_id, d.producto_variante_id, d.variante_produccion_id
            )
            if fila is None:
                fila = Existencia(
                    almacen_id=d.almacen_id, producto_id=d.producto_id,
                    producto_variante_id=d.producto_variante_id,
                    variante_produccion_id=d.variante_produccion_id, stock=0, cantidad=Decimal("0"),
                )
                nuevas.append(fila)
            filas[llave] = fila
        fila = filas[llave]
        antes = Decimal(str(fila.cantidad or 0))
        fila.cantidad = antes + d.cantidad
        entradas.append((d, MovimientoInventarioDetalle(
            producto_id=d.producto_id, producto_variante_id=d.producto_variante_id,
            variante_produccion_id=d.variante_produccion_id, almacen_id=d.almacen_id,
            cantidad=d.cantidad, costo_unitario=d.costo_unitario,
            cantidad_antes=antes, cantidad_despues=fila.cantidad,
        )))
    if not entradas:
        return []
    for fila in filas.values():
        if fila.pk:
            fila.save(update_fields=["cantidad"])
        else:
            fila.save()

    packing = despacho.packing
    mov = MovimientoInventario.objects.create(
        empresa_id=salida.empresa_id, sucursal_id=salida.sucursal_id, pedido_id=salida.pedido_id,
        despacho=despacho, tipo_movimiento=TipoMovimiento.ENTRADA, usuario=user,
        observaciones=f"Cancelación del despacho {despacho.pk} del pedido "
                      f"{packing.pedido.folio or packing.pedido_id}"[:150],
    )
    nuevos = [e for _, e in entradas]
    for e in nuevos:
        e.movimiento_inventario = mov
    MovimientoInventarioDetalle.objects.bulk_create(nuevos)
    # Regresa al costo con el que salió: no mueve el promedio por una reversa.
    costeo.costear(salida.empresa_id, [(e, e.cantidad, s.costo_unitario) for s, e in entradas],
                   costeo.Origen.PEDIDO)
    return [
        {"producto_id": e.producto_id, "producto_variante_id": e.producto_variante_id,
         "variante_produccion_id": e.variante_produccion_id, "almacen_id": e.almacen_id,
         "cantidad": str(e.cantidad)}
        for e in nuevos
    ]


@transaction.atomic
def revertir_transferencia(transferencia, request, motivo):
    """Una TR ejecutada no se edita: se crea la TR inversa (destino -> origen, mismas
    piezas, folio propio, ``reversa_de``) y la original queda CANCELADA con el motivo.
    Sólo TR manuales o de un picking ya cancelado: las de un picking vivo son el
    surtido de ese picking y se deshacen cancelándolo primero."""
    from wms.services.transferencia_service import TransferenciaService

    tr = Transferencia.objects.select_for_update(of=("self",)).get(pk=transferencia.pk)
    if tr.status != Transferencia.TransferenciaStatus.COMPLETADA:
        raise ValidationError({"transferencia": f"Sólo se revierte una TR completada (esta está {tr.status})."})
    if tr.reversa_de_id:
        raise ValidationError({"transferencia": "Esta TR ya es una reversa; no se revierte."})
    if Transferencia.objects.filter(reversa_de=tr).exists():
        raise ValidationError({"transferencia": "La TR ya fue revertida."})
    if tr.picking_id and Picking.objects.filter(pk=tr.picking_id).exclude(estado=Picking.Estado.CANCELADO).exists():
        raise ValidationError({"transferencia": "La TR es el surtido de un picking vivo. Cancela el picking primero."})

    renglones = [
        {
            "producto": d.producto, "producto_variante": d.producto_variante,
            "variante_produccion": d.variante_produccion, "cantidad": d.cantidad,
            "ubicacion_origen": d.ubicacion_destino, "ubicacion_destino": d.ubicacion_origen,
            "lote": d.lote, "serie": d.serie,
        }
        for d in tr.transferencia_detalle.select_related(
            "producto", "producto_variante", "variante_produccion", "ubicacion_origen",
            "ubicacion_destino", "lote", "serie",
        ).order_by("pk")
    ]
    reversa = TransferenciaService.handle_store(
        {
            "almacen_origen": tr.almacen_destino, "almacen_destino": tr.almacen_origen,
            "observaciones": f"Reversa de {tr.folio}: {motivo}"[:500],
            "transferencia_detalle": renglones,
        },
        request.user, request=request, empresa=tr.empresa, sucursal=tr.sucursal,
    )
    reversa.reversa_de = tr
    reversa.picking_id = tr.picking_id
    reversa.save(update_fields=["reversa_de", "picking"])
    _marcar(tr, "status", Transferencia.TransferenciaStatus.CANCELADA, request.user, motivo)
    _auditar(request, tr.empresa_id, "transferencias", tr,
             {"folio": tr.folio, "status": Transferencia.TransferenciaStatus.COMPLETADA},
             {"folio": tr.folio, "status": tr.status, "motivo": motivo,
              "reversa_id": reversa.pk, "reversa_folio": reversa.folio})
    return tr, reversa
