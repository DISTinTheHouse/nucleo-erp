"""Señales que llenan ``BitacoraDocumento``.

Costo: 0 consultas para conocer el estado previo (se recuerda en ``post_init``
leyendo solo atributos ya cargados) y 1 INSERT únicamente cuando hay algo que
registrar: alta, cambio de estatus, baja, restauración o borrado. Un ``save()``
que no cambia el estatus no escribe nada.

Límites conocidos:
- ``QuerySet.update()`` y ``bulk_create`` no disparan señales: esos cambios no
  quedan en la bitácora.
- ``wms.Picking``, ``Packing`` y ``Transferencia`` no tienen ``activo``: nunca
  registran baja ni restauración, solo cambios de estatus.
"""
import logging
import re

from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_delete, post_init, post_save

logger = logging.getLogger(__name__)


class Doc:
    """Cómo leer un documento: tipo, campos de folio/estatus/total y sus orígenes.

    ``origenes`` es una lista de ``(tipo, campo)``; se usa el primero con valor
    (p. ej. un movimiento de inventario puede venir de pedido, recepción, OP...).
    """

    __slots__ = ("tipo", "folio", "estatus", "total", "origenes")

    def __init__(self, tipo, folio=None, estatus=None, total=None, origenes=()):
        self.tipo, self.folio, self.estatus, self.total, self.origenes = tipo, folio, estatus, total, origenes


DOCUMENTOS = {
    "ventas.Pedido": Doc("pedido", "folio", "estatus", "gran_total"),
    "compras.OrdenCompra": Doc("orden_compra", "folio", "estatus", "gran_total", [("pedido", "pedido_id")]),
    "compras.Recepcion": Doc("recepcion", "folio", "estatus", None,
                             [("orden_compra", "orden_compra_id"), ("orden_produccion", "op_id")]),
    "produccion.OrdenProduccion": Doc("orden_produccion", "folio_op", "estatus_op", None, [("pedido", "pedido_id")]),
    "produccion.OrdenesBordado": Doc("orden_bordado", "folio_bordado", "estatus_bordado", None, [("pedido", "pedido_id")]),
    "produccion.OrdenesReflejante": Doc("orden_reflejante", "folio_reflejante", "estatus_reflejante", None, [("pedido", "pedido_id")]),
    "produccion.OrdenesCorteManga": Doc("orden_corte_manga", "folio_ocm", "estatus_corte", None, [("pedido", "pedido_id")]),
    "produccion.SalidaMaquila": Doc("salida_maquila", "folio", "estatus", "total", [("orden_produccion", "op_id")]),
    "wms.Picking": Doc("picking", "folio", "estado", None, [("pedido", "pedido_id")]),
    "wms.Packing": Doc("packing", "folio", "estado", None, [("pedido", "pedido_id")]),
    "wms.Transferencia": Doc("transferencia", "folio", "status"),
    "finanzas.Factura": Doc("factura", "folio", "estatus", "total", [("pedido", "pedido_id")]),
    "finanzas.FacturaProveedor": Doc("factura_proveedor", "folio", "estatus", "total",
                                     [("recepcion", "recepcion_id"), ("orden_compra", "oc_id")]),
    "finanzas.Cobro": Doc("cobro", "folio", "estatus", "total_cobrado"),
    "finanzas.Pago": Doc("pago", "referencia", "estatus", "total_pagado"),
    "finanzas.Poliza": Doc("poliza", "folio", "estatus"),
    "inventarios.MovimientoInventario": Doc(
        "movimiento_inventario", None, "tipo_movimiento", None,
        [("pedido", "pedido_id"), ("recepcion", "recepcion_id"), ("transferencia", "transferencia_id"),
         ("orden_produccion", "op_id"), ("factura", "factura_id")],
    ),
    "inventarios.AjusteInventario": Doc("ajuste_inventario"),
}

#: tipo -> etiqueta del modelo, para validar ``?tipo=`` y buscar el documento.
TIPOS = {doc.tipo: label for label, doc in DOCUMENTOS.items()}

_PREVIO = "_bitacora_previo"

# Etiquetas uniformes (#1065): formato oración y con acentos.
_ACENTOS = {
    "preparacion": "preparación", "produccion": "producción", "revision": "revisión",
    "autorizacion": "autorización", "recepcion": "recepción", "cancelacion": "cancelación",
    "liberacion": "liberación", "confeccion": "confección", "transito": "tránsito",
    "aplicacion": "aplicación", "inspeccion": "inspección", "devolucion": "devolución",
    "validacion": "validación", "facturacion": "facturación", "programacion": "programación",
    "fabricacion": "fabricación", "planeacion": "planeación", "asignacion": "asignación",
}


def etiqueta_uniforme(texto):
    """``"POR AUTORIZAR"`` → ``"Por autorizar"``; ``"Preparacion"`` → ``"Preparación"``;
    quita sufijos ``(legacy)`` y guiones bajos."""
    if texto is None:
        return ""
    texto = re.sub(r"\s*\(legacy\)\s*$", "", str(texto), flags=re.I).replace("_", " ").strip()
    if not texto:
        return ""
    palabras = [_ACENTOS.get(p.lower(), p.lower()) for p in texto.split()]
    frase = " ".join(palabras)
    return frase[:1].upper() + frase[1:]


def _etiqueta(modelo, doc, valor):
    if valor is None or valor == "" or not doc.estatus:
        return ""
    opciones = dict(modelo._meta.get_field(doc.estatus).flatchoices or ())
    return etiqueta_uniforme(opciones.get(valor, valor))


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


def _origen(obj, doc):
    for tipo, campo in doc.origenes:
        valor = getattr(obj, campo, None)
        if valor:
            return tipo, valor
    return "", None


def _registrar(obj, doc, accion, anterior=None, nuevo=None):
    from .models import BitacoraDocumento

    empresa_id = getattr(obj, "empresa_id", None)
    if not empresa_id:
        return
    modelo = type(obj)
    origen_tipo, origen_id = _origen(obj, doc)
    try:
        with transaction.atomic():  # savepoint: un fallo aquí no tumba el documento
            BitacoraDocumento.objects.create(
                empresa_id=empresa_id,
                sucursal_id=getattr(obj, "sucursal_id", None),
                tipo_documento=doc.tipo,
                documento_id=str(obj.pk),
                folio=str((getattr(obj, doc.folio, None) if doc.folio else None) or "")[:60],
                accion=accion,
                estatus_anterior=_etiqueta(modelo, doc, anterior)[:40],
                estatus_nuevo=_etiqueta(modelo, doc, nuevo)[:40],
                estatus_codigo="" if nuevo is None else str(nuevo)[:40],
                total=getattr(obj, doc.total, None) if doc.total else None,
                moneda_id=getattr(obj, "moneda_id", None),
                origen_tipo=origen_tipo,
                origen_id=origen_id,
                usuario_id=_usuario_id(),
            )
    except Exception:
        logger.exception("No se pudo registrar %s %s en la bitácora", doc.tipo, obj.pk)


def _al_iniciar(sender, instance, **kwargs):
    doc = DOCUMENTOS[sender._meta.label]
    cargados = instance.__dict__  # sin consultas diferidas
    setattr(instance, _PREVIO, (cargados.get(doc.estatus) if doc.estatus else None, cargados.get("activo")))


def _al_guardar(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    doc = DOCUMENTOS[sender._meta.label]
    est_prev, activo_prev = getattr(instance, _PREVIO, (None, None))
    est = instance.__dict__.get(doc.estatus) if doc.estatus else None
    activo = instance.__dict__.get("activo")
    if created:
        _registrar(instance, doc, "crear", nuevo=est)
    else:
        if doc.estatus and est_prev is not None and est != est_prev:
            _registrar(instance, doc, "estatus", est_prev, est)
        if activo_prev is True and activo is False:
            _registrar(instance, doc, "baja", est, est)
        elif activo_prev is False and activo is True:
            _registrar(instance, doc, "restaurar", est, est)
    setattr(instance, _PREVIO, (est, activo))


def _al_borrar(sender, instance, **kwargs):
    doc = DOCUMENTOS[sender._meta.label]
    est = instance.__dict__.get(doc.estatus) if doc.estatus else None
    _registrar(instance, doc, "eliminar", est, None)


def conectar():
    for label in DOCUMENTOS:
        modelo = apps.get_model(label)
        uid = f"bitacora:{label}"
        post_init.connect(_al_iniciar, sender=modelo, dispatch_uid=uid + ":init", weak=False)
        post_save.connect(_al_guardar, sender=modelo, dispatch_uid=uid + ":save", weak=False)
        post_delete.connect(_al_borrar, sender=modelo, dispatch_uid=uid + ":delete", weak=False)
