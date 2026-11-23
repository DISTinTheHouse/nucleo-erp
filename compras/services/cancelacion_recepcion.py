"""Regla única para cancelar una recepción antes de calidad (#1307, EC-926).

La usan ``POST recepciones/{id}/cancelar/`` (con bloqueos, dentro de su
transacción) y ``puede_cancelar`` del retrieve (sin bloqueos), para que el botón
del frontend y la acción no puedan divergir:

- Solo admin de la empresa o superusuario.
- Ya ``CANCELADA``: no.
- Antes de calidad: ``EN_CALIDAD`` (cualquier origen) o ``CERRADA`` si es de SM
  de OB/OR (``tipo_origen == "SM"``, se crea así), sin inspección ni movimiento
  de inventario.
- OP ``COMPLETADO``: no.
- Factura de proveedor sin cancelar (dada de baja o no): no, salvo la de
  maquila en Borrador, que la cancelación anula.
"""

from django.db.models import Exists, OuterRef
from rest_framework.exceptions import ValidationError

from compras.models import CalidadInspeccion, Recepcion
from finanzas.models import FacturaProveedor
from inventarios.models import MovimientoInventario
from produccion.models import OrdenProduccion

# ``Recepcion.motivo_cancelacion`` (como ``Pedido.motivo_cancelacion``).
MOTIVO_CANCELACION_MAX = 500

# Anotaciones del retrieve (``anotar_bloqueos_cancelacion``): con ellas la regla
# no consulta; sin ellas (``cancelar/``, bajo sus bloqueos) consulta como siempre.
ANOT_INSPECCION = "cancelacion_tiene_inspeccion"
ANOT_MOVIMIENTO = "cancelacion_tiene_movimiento"
ANOT_FACTURA_VIVA = "cancelacion_tiene_factura_viva"


def puede_cancelar_recepciones(user):
    """Paso 1 de la regla: quién puede cancelar."""
    return bool(user.is_superuser or getattr(user, "is_admin_empresa", False))


def _facturas_sin_cancelar(recepcion_ref):
    Factura = FacturaProveedor.FacturaProveedorStatus
    return FacturaProveedor.objects.filter(recepcion=recepcion_ref).exclude(estatus=Factura.CANCELADA)


def _bloquea(factura):
    """Una factura sin cancelar bloquea, salvo la de maquila en Borrador."""
    return not (factura.salida_maquila_id and factura.estatus == FacturaProveedor.FacturaProveedorStatus.BORRADOR)


def anotar_bloqueos_cancelacion(qs):
    """Los tres bloqueos que consultan la BD, como ``Exists`` en la misma consulta
    (FK indexadas a ``recepcion``). La factura viva sigue ``_bloquea``."""
    Factura = FacturaProveedor.FacturaProveedorStatus
    return qs.annotate(**{
        ANOT_INSPECCION: Exists(CalidadInspeccion.objects.filter(recepcion=OuterRef("pk"))),
        ANOT_MOVIMIENTO: Exists(MovimientoInventario.objects.filter(recepcion=OuterRef("pk"))),
        # NOT (de maquila AND Borrador), igual que ``_bloquea``.
        ANOT_FACTURA_VIVA: Exists(
            _facturas_sin_cancelar(OuterRef("pk")).exclude(salida_maquila__isnull=False, estatus=Factura.BORRADOR)
        ),
    })


def _anotado_o(recepcion, nombre, consulta):
    valor = getattr(recepcion, nombre, None)
    return consulta() if valor is None else valor


def validar_cancelacion_recepcion(recepcion, op=None, *, bloquear=False):
    """Estado de la recepción que impide cancelarla: lanza ``ValidationError``
    con la llave y el mensaje que responde ``cancelar/``.

    ``op`` es la OP de la recepción (la ya bloqueada en ``cancelar/``; si no se
    pasa se usa ``recepcion.op``). Regresa las facturas de proveedor sin
    cancelar (las de maquila en Borrador, que la cancelación anula); con
    ``bloquear`` se leen con ``select_for_update``. Consultas en el orden de la
    regla y solo si las anteriores no bloquearon.

    Si la recepción trae las anotaciones de ``anotar_bloqueos_cancelacion`` (el
    retrieve, nunca con ``bloquear``) se usan en vez de consultar; entonces no
    hay lista de facturas (regresa ``[]``) y el mensaje de la factura es genérico:
    solo cuenta si bloquea.
    """
    E = Recepcion.EstatusRecepcion
    if recepcion.estatus == E.CANCELADA:
        raise ValidationError({"estatus": "La recepción ya está cancelada."})
    antes_de_calidad = recepcion.estatus == E.EN_CALIDAD or (
        recepcion.tipo_origen == Recepcion.TipoOrigen.SALIDA_MAQUILA and recepcion.estatus == E.CERRADA
    )
    if (
        not antes_de_calidad
        or _anotado_o(recepcion, ANOT_INSPECCION, CalidadInspeccion.objects.filter(recepcion=recepcion).exists)
        or _anotado_o(recepcion, ANOT_MOVIMIENTO, MovimientoInventario.objects.filter(recepcion=recepcion).exists)
    ):
        raise ValidationError({"estatus": (
            "La recepción ya pasó por calidad y no puede cancelarse: lo aprobado ya es existencia."
        )})
    if op is None and recepcion.op_id:
        op = recepcion.op
    if op is not None and op.estatus_op == OrdenProduccion.EstatusOrdenProduccion.COMPLETADO:
        raise ValidationError({"op": f"La OP {op.folio_op} ya está completada y la recepción no puede cancelarse."})
    # Facturas sin cancelar, dadas de baja o no (como ``OrdenCompraViewSet.cancelar``).
    factura_viva = None if bloquear else getattr(recepcion, ANOT_FACTURA_VIVA, None)
    if factura_viva is not None:
        if factura_viva:
            raise ValidationError({"facturas_proveedores": (
                "La recepción tiene una factura de proveedor sin cancelar: cancélala primero en Finanzas."
            )})
        return []
    facturas_qs = _facturas_sin_cancelar(recepcion)
    if bloquear:
        facturas_qs = facturas_qs.select_for_update()
    facturas = list(facturas_qs.order_by("id"))
    for factura in facturas:
        if _bloquea(factura):
            raise ValidationError({"facturas_proveedores": (
                f"La factura de proveedor {factura.folio or factura.pk} de la recepción está "
                f"{factura.estatus.lower()}: cancélala primero en Finanzas."
            )})
    return facturas


def puede_cancelar_recepcion(user, recepcion):
    """``puede_cancelar`` del retrieve: la misma regla que ``cancelar/``, para
    ``user`` y en su empresa activa."""
    if not puede_cancelar_recepciones(user):
        return False
    empresa_id = getattr(user, "empresa_id", None)
    if not empresa_id or recepcion.empresa_id != empresa_id or not recepcion.activo:
        return False
    try:
        validar_cancelacion_recepcion(recepcion)
    except ValidationError:
        return False
    return True
