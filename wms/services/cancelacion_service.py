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
# Texto con que el código crea las TR de un picking (alta y traslados, EC-916).
from wms.services.transferencia_picking_service import PATRON as PATRON_TR_DE_PICKING

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
    if despacho.fecha_entrega:  # #1361
        raise ValidationError({"despacho": "El despacho ya se entregó; registra una devolución."})

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


@transaction.atomic
def entregar_despacho(despacho, request, recibio="", fecha=None):
    """#1361: señal de entrega. Marca el despacho entregado (fecha, quién lo registró y
    quién recibió); desde ahí ya no se cancela. Una vez, sin cambiar inventario."""
    despacho = Despacho.objects.select_for_update(of=("self",)).get(pk=despacho.pk)
    if despacho.estado == Despacho.Estado.CANCELADO:
        raise ValidationError({"despacho": "El despacho está cancelado."})
    if despacho.fecha_entrega:
        raise ValidationError({"despacho": "El despacho ya está marcado como entregado."})
    ahora = timezone.now()
    fecha = fecha or ahora
    if fecha > ahora:
        raise ValidationError({"fecha_entrega": "La fecha de entrega no puede ser futura."})
    despacho.fecha_entrega, despacho.entregado_por, despacho.recibio = fecha, request.user, (recibio or "")[:150]
    despacho.save(update_fields=["fecha_entrega", "entregado_por", "recibio"])
    meta = getattr(request, "META", None) or {}
    AuditoriaEvento.objects.create(
        empresa_id=despacho.packing.empresa_id, usuario=request.user, modulo="wms", accion="ENTREGAR",
        tabla="despachos", id_registro=str(despacho.pk), antes_json={"fecha_entrega": None},
        despues_json={"fecha_entrega": fecha.isoformat(), "recibio": despacho.recibio},
        ip=ip_auditoria(request), user_agent=meta.get("HTTP_USER_AGENT") or None,
    )
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


def _picking_id_de(tr):
    """Picking de la TR: ``Transferencia.picking`` (EC-916) o, en una TR sin esa FK
    (anterior al backfill o ambigua), el picking cuya ``transferencia`` es ella.
    ``None``: TR manual (o de picking sin ligar: ``revertir_transferencia`` la rechaza)."""
    if tr.picking_id:
        return tr.picking_id
    return Picking.objects.filter(transferencia_id=tr.pk).order_by("pk").values_list("pk", flat=True).first()


def _tr_posterior_viva(tr, picking):
    """Folio de la última TR viva del picking posterior a ``tr`` (``None`` si ``tr`` es
    la última). Viva = ``COMPLETADA`` y no reversa; las revertidas quedan CANCELADA.
    Por ``pk``, no por fecha. ``Picking.transferencia`` (la última TR del picking)
    cuenta aunque no tenga la FK ``picking``."""
    return (
        Transferencia.objects.filter(Q(picking_id=picking.pk) | Q(pk=picking.transferencia_id))
        .filter(pk__gt=tr.pk, reversa_de__isnull=True, status=Transferencia.TransferenciaStatus.COMPLETADA)
        .order_by("-pk")
        .values_list("folio", flat=True)
        .first()
    )


def _movimientos_ajenos_posteriores(tr, picking_id):
    """#1362: la reversa saca del destino lo que haya del SKU, no lo que trajo esta TR.
    Si después de ``tr`` algo ajeno (otra TR, picking, despacho, recepción, ajuste) movió
    esos SKU en el almacén destino, regresa su descripción y la reversa se bloquea.
    No cuentan las TR de la cadena del mismo picking ni sus reversas. 2 consultas."""
    from wms.models import TransferenciaDetalle

    claves = set(tr.transferencia_detalle.values_list("producto_id", "producto_variante_id", "variante_produccion_id"))
    if not claves:
        return None
    q_sku = Q()
    for p, pv, vp in claves:
        q_sku |= Q(producto_id=p, producto_variante_id=pv, variante_produccion_id=vp)
    destino = tr.almacen_destino_id
    propias = Q(pk=tr.pk)
    if picking_id:
        propias |= Q(picking_id=picking_id) | Q(reversa_de__picking_id=picking_id)
    tr_ajena = (
        Transferencia.objects.filter(empresa_id=tr.empresa_id, pk__gt=tr.pk)
        .filter(Q(almacen_origen_id=destino) | Q(almacen_destino_id=destino))
        .exclude(propias)
        .filter(pk__in=TransferenciaDetalle.objects.filter(q_sku).values("transferencia_id"))
        .order_by("pk").values_list("folio", flat=True).first()
    )
    if tr_ajena:
        return f"la TR {tr_ajena}"
    desde = (
        MovimientoInventario.objects.filter(transferencia=tr).order_by("pk").values_list("pk", flat=True).first()
    )
    if desde is None:
        return None
    ajeno = (
        MovimientoInventarioDetalle.objects.filter(q_sku, movimiento_inventario_id__gt=desde)
        .filter(Q(almacen_id=destino) | Q(ubicacion_origen__almacen_id=destino) | Q(ubicacion_destino__almacen_id=destino))
        .filter(movimiento_inventario__transferencia__isnull=True, movimiento_inventario__activo=True)
        .order_by("pk").values_list("movimiento_inventario_id", "movimiento_inventario__tipo_movimiento").first()
    )
    return f"el movimiento {ajeno[1].lower()} #{ajeno[0]}" if ajeno else None


@transaction.atomic
def revertir_transferencia(transferencia, request, motivo):
    """Una TR ejecutada no se edita: se crea la TR inversa (destino -> origen, mismas
    piezas, folio propio, ``reversa_de``) y la original queda CANCELADA con el motivo.
    Sólo TR manuales o de un picking ya cancelado: las de un picking vivo son el
    surtido de ese picking y se deshacen cancelándolo primero.

    Cadena del picking (EC-949): sólo se revierte su última TR viva, de la más nueva
    a la más vieja; revertir una vieja movería del destino piezas que ya no están
    ahí (o ajenas del mismo SKU). Al revertir, el picking queda en el origen de la TR
    revertida (``almacen``), que es donde quedan sus prendas. Las TR manuales no
    tienen cadena.

    Mismo orden de bloqueo que el alta y las cancelaciones (EC-574): Pedido ->
    Picking -> TR. El picking se resuelve sin bloquear y todo se revalida bajo lock."""
    from wms.services.transferencia_service import TransferenciaService

    tr_cambio = ValidationError({"transferencia": "La TR cambió mientras se revertía. Intenta de nuevo."})
    picking_id = _picking_id_de(transferencia)
    picking = None
    if picking_id:
        _bloquear_pedido(Picking.objects.filter(pk=picking_id).values_list("pedido_id", flat=True).first())
        picking = Picking.objects.select_for_update(of=("self",)).filter(pk=picking_id).first()
        if picking is None:
            # El picking se borró (p. ej. en cascada con su pedido) después de leerlo.
            raise tr_cambio
    tr = Transferencia.objects.select_for_update(of=("self",)).get(pk=transferencia.pk)
    if _picking_id_de(tr) != picking_id:
        # Otro proceso ligó la TR a un picking entre la lectura y el lock.
        raise tr_cambio
    if tr.status != Transferencia.TransferenciaStatus.COMPLETADA:
        raise ValidationError({"transferencia": f"Sólo se revierte una TR completada (esta está {tr.status})."})
    if tr.reversa_de_id:
        raise ValidationError({"transferencia": "Esta TR ya es una reversa; no se revierte."})
    if Transferencia.objects.filter(reversa_de=tr).exists():
        raise ValidationError({"transferencia": "La TR ya fue revertida."})
    if picking is None and PATRON_TR_DE_PICKING.match(tr.observaciones or ""):
        # TR de picking sin ligar (``ligar_transferencias_picking`` la dejó en null por
        # ambigua): sin su picking no se puede revisar la cadena, el picking vivo ni su
        # almacén. No se revierte como manual.
        raise ValidationError({"transferencia": (
            f"La TR {tr.folio} es de un picking pero no está ligada a él. "
            "No se puede revertir hasta ligarla a su picking."
        )})
    if picking is not None:
        if picking.estado != Picking.Estado.CANCELADO:
            raise ValidationError(
                {"transferencia": "La TR es el surtido de un picking vivo. Cancela el picking primero."}
            )
        posterior = _tr_posterior_viva(tr, picking)
        if posterior:
            raise ValidationError({"transferencia": (
                f"Sólo se revierte la última TR del picking {picking.folio}. Revierte primero {posterior}."
            )})

    ajeno = _movimientos_ajenos_posteriores(tr, picking_id)
    if ajeno:
        raise ValidationError({"transferencia": (
            f"Después de {tr.folio} hubo movimientos ajenos de esos artículos en el almacén destino "
            f"({ajeno}). La reversa se llevaría piezas que no son de esta TR: haz una TR manual o un ajuste."
        )})

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
    try:
        reversa = TransferenciaService.handle_store(
            {
                "almacen_origen": tr.almacen_destino, "almacen_destino": tr.almacen_origen,
                "observaciones": f"Reversa de {tr.folio}: {motivo}"[:500],
                "transferencia_detalle": renglones,
            },
            request.user, request=request, empresa=tr.empresa, sucursal=tr.sucursal,
        )
    except ValidationError as exc:
        # #1360: ``handle_store`` lanza textos sueltos (lista plana); aquí, dict como el resto.
        if isinstance(exc.detail, dict):
            raise
        raise ValidationError({"transferencia": " ".join(str(m) for m in exc.detail)})
    reversa.reversa_de = tr
    reversa.picking_id = picking_id
    reversa.save(update_fields=["reversa_de", "picking"])
    _marcar(tr, "status", Transferencia.TransferenciaStatus.CANCELADA, request.user, motivo)
    antes = {"folio": tr.folio, "status": Transferencia.TransferenciaStatus.COMPLETADA}
    despues = {"folio": tr.folio, "status": tr.status, "motivo": motivo,
               "reversa_id": reversa.pk, "reversa_folio": reversa.folio}
    if picking is not None:
        # Las prendas vuelven al origen de la TR: ahí queda el picking. ``almacen_destino``
        # y ``transferencia`` no se tocan; la TR del picking ya no está COMPLETADA, así
        # que ``Picking.almacen_actual`` (y el filtro del listado) dan ``almacen``.
        antes["picking_almacen_id"] = picking.almacen_id
        picking.almacen = tr.almacen_origen
        picking.save(update_fields=["almacen", "updated_at"])
        despues["picking_almacen_id"] = picking.almacen_id
    _auditar(request, tr.empresa_id, "transferencias", tr, antes, despues)
    return tr, reversa
