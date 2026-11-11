"""Señales que llenan ``BitacoraDocumento``.

Costo: 0 consultas para conocer el estado previo (se recuerda en ``post_init``
leyendo solo atributos ya cargados) y 1 INSERT únicamente cuando hay algo que
registrar: alta, cambio de estatus, baja, restauración o borrado. Un ``save()``
que no cambia el estatus no escribe nada.

Límite conocido: ``QuerySet.update()`` y ``bulk_create`` no disparan señales,
así que esos cambios no quedan en la bitácora.
"""
import logging

from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_delete, post_init, post_save

logger = logging.getLogger(__name__)

# modelo -> (tipo, campo folio, campo estatus, campo total, origen (tipo, campo))
DOCUMENTOS = {
    "ventas.Pedido": ("pedido", "folio", "estatus", "gran_total", None),
    "compras.OrdenCompra": ("orden_compra", "folio", "estatus", "gran_total", ("pedido", "pedido_id")),
    "compras.Recepcion": ("recepcion", "folio", "estatus", None, ("orden_compra", "orden_compra_id")),
    "produccion.OrdenProduccion": ("orden_produccion", "folio_op", "estatus_op", None, ("pedido", "pedido_id")),
    "produccion.OrdenesBordado": ("orden_bordado", "folio_bordado", "estatus_bordado", None, ("pedido", "pedido_id")),
    "produccion.OrdenesReflejante": ("orden_reflejante", "folio_reflejante", "estatus_reflejante", None, ("pedido", "pedido_id")),
    "produccion.OrdenesCorteManga": ("orden_corte_manga", "folio_ocm", "estatus_corte", None, ("pedido", "pedido_id")),
    "wms.Picking": ("picking", "folio", "estado", None, ("pedido", "pedido_id")),
    "wms.Packing": ("packing", "folio", "estado", None, ("pedido", "pedido_id")),
    "wms.Transferencia": ("transferencia", "folio", "status", None, None),
    "finanzas.Factura": ("factura", "folio", "estatus", "total", ("pedido", "pedido_id")),
    "finanzas.FacturaProveedor": ("factura_proveedor", "folio", "estatus", "total", None),
    "finanzas.Cobro": ("cobro", "folio", "estatus", "total_cobrado", None),
    "finanzas.Pago": ("pago", None, "estatus", "total_pagado", None),
    "finanzas.Poliza": ("poliza", "folio", "estatus", None, None),
    "inventarios.MovimientoInventario": ("movimiento_inventario", None, "tipo_movimiento", None, ("pedido", "pedido_id")),
    "inventarios.AjusteInventario": ("ajuste_inventario", None, None, None, None),
}

_PREVIO = "_bitacora_previo"


def _valor(obj, campo):
    return getattr(obj, campo, None) if campo else None


def _usuario_id():
    """Usuario de la petición en curso: ``simple_history`` guarda la petición en
    un thread-local y DRF le copia el usuario ya autenticado por JWT."""
    try:
        from simple_history.models import HistoricalRecords

        request = getattr(HistoricalRecords.context, "request", None)
        user = getattr(request, "user", None)
        return user.pk if getattr(user, "is_authenticated", False) else None
    except Exception:
        return None


def _etiqueta(obj, conf, valor):
    """Etiqueta legible del estatus (``choices``) o el valor tal cual."""
    if valor is None or valor == "" or not conf[2]:
        return valor
    opciones = dict(type(obj)._meta.get_field(conf[2]).flatchoices or ())
    return opciones.get(valor, valor)


def _registrar(obj, conf, accion, anterior="", nuevo=""):
    from .models import BitacoraDocumento

    tipo, f_folio, _f_est, f_total, origen = conf
    empresa_id = getattr(obj, "empresa_id", None)
    if not empresa_id:
        return
    origen_id = getattr(obj, origen[1], None) if origen else None
    try:
        with transaction.atomic():  # savepoint: un fallo aquí no tumba el documento
            BitacoraDocumento.objects.create(
                empresa_id=empresa_id,
                sucursal_id=getattr(obj, "sucursal_id", None),
                tipo_documento=tipo,
                documento_id=str(obj.pk),
                folio=str(_valor(obj, f_folio) or "")[:60],
                accion=accion,
                estatus_anterior=str(_etiqueta(obj, conf, anterior) or "")[:40],
                estatus_nuevo=str(_etiqueta(obj, conf, nuevo) or "")[:40],
                total=_valor(obj, f_total),
                moneda_id=getattr(obj, "moneda_id", None),
                origen_tipo=origen[0] if origen_id else "",
                origen_id=origen_id,
                usuario_id=_usuario_id(),
            )
    except Exception:
        logger.exception("No se pudo registrar %s %s en la bitácora", tipo, obj.pk)


def _al_iniciar(sender, instance, **kwargs):
    conf = DOCUMENTOS[sender._meta.label]
    cargados = instance.__dict__  # sin consultas diferidas
    setattr(instance, _PREVIO, (cargados.get(conf[2]) if conf[2] else None, cargados.get("activo")))


def _al_guardar(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    conf = DOCUMENTOS[sender._meta.label]
    est_prev, activo_prev = getattr(instance, _PREVIO, (None, None))
    est = instance.__dict__.get(conf[2]) if conf[2] else None
    activo = instance.__dict__.get("activo")
    if created:
        _registrar(instance, conf, "crear", nuevo=est)
    else:
        if conf[2] and est_prev is not None and est != est_prev:
            _registrar(instance, conf, "estatus", est_prev, est)
        if activo_prev is True and activo is False:
            _registrar(instance, conf, "baja", est, est)
        elif activo_prev is False and activo is True:
            _registrar(instance, conf, "restaurar", est, est)
    setattr(instance, _PREVIO, (est, activo))


def _al_borrar(sender, instance, **kwargs):
    conf = DOCUMENTOS[sender._meta.label]
    est = instance.__dict__.get(conf[2]) if conf[2] else None
    _registrar(instance, conf, "eliminar", est, "")


def conectar():
    for label in DOCUMENTOS:
        modelo = apps.get_model(label)
        uid = f"bitacora:{label}"
        post_init.connect(_al_iniciar, sender=modelo, dispatch_uid=uid + ":init", weak=False)
        post_save.connect(_al_guardar, sender=modelo, dispatch_uid=uid + ":save", weak=False)
        post_delete.connect(_al_borrar, sender=modelo, dispatch_uid=uid + ":delete", weak=False)
