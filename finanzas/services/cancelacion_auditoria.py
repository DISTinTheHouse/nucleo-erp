"""Rastro de las cancelaciones de finanzas: motivo obligatorio, permiso por tipo,
quién y cuándo en el documento y ``AuditoriaEvento``.

Los servicios de cada documento siguen haciendo la reversa contable/bancaria; esto
se llama en la misma transacción, después del servicio.
"""
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from auditoria.models import AuditoriaEvento
from nucleo.middleware import ip_auditoria
from nucleo.motivo import exigir_largo_motivo

PERMISO_FACTURAS = "E-FIN-FACTURAS"
PERMISO_POLIZAS = "CANCELAR-FIN-POLIZAS"
PERMISO_COBROS = "CANCELAR-FIN-COBROS"
PERMISO_PAGOS = "CANCELAR-FIN-PAGOS"
PERMISO_BANCOS = "CANCELAR-FIN-BANCOS"



def motivo_requerido(request):
    data = request.data if hasattr(request.data, "get") else {}
    motivo = data.get("motivo") or request.query_params.get("motivo")
    if not isinstance(motivo, str) or not motivo.strip():
        raise ValidationError({"motivo": "Indica el motivo de la cancelación."})
    motivo = motivo.strip()
    return exigir_largo_motivo(motivo)  # #1399: más de 500 → 400


def exigir_permiso(user, clave):
    """Superusuario y admin de empresa pasan (``tiene_permiso``)."""
    if not user.tiene_permiso(clave):
        raise PermissionDenied(f"No tienes permiso para cancelar este documento ({clave}).")


CANCELADOS = ("Cancelada", "Cancelado")


def exigir_no_cancelado(doc):
    """Repetir la cancelación no debe reescribir motivo/quién/cuándo."""
    if getattr(doc, "estatus", None) in CANCELADOS:
        raise ValidationError({"estatus": "El documento ya está cancelado."})


def empresa_de(doc):
    for ruta in (("empresa_id",), ("cuenta_bancaria", "empresa_id"), ("factura", "empresa_id")):
        valor = doc
        for attr in ruta:
            valor = getattr(valor, attr, None)
            if valor is None:
                break
        if valor is not None:
            return valor
    return None


def _usuario(user):
    return user if getattr(user, "pk", None) else None


def registrar_cancelacion(doc, request, motivo, estatus_antes, extra=None, accion="CANCELAR"):
    """Sella motivo/fecha/quién en el documento (``update``: no reabre la fila que el
    servicio ya guardó) y deja el evento de auditoría."""
    user = _usuario(request.user)
    ahora = timezone.now()
    type(doc).objects.filter(pk=doc.pk).update(
        motivo_cancelacion=motivo, fecha_cancelacion=ahora, cancelado_por=user,
    )
    doc.motivo_cancelacion, doc.fecha_cancelacion, doc.cancelado_por = motivo, ahora, user
    AuditoriaEvento.objects.create(
        empresa_id=empresa_de(doc), usuario=user, modulo="finanzas", accion=accion,
        tabla=doc._meta.db_table, id_registro=str(doc.pk),
        antes_json={"estatus": estatus_antes, "folio": getattr(doc, "folio", None)},
        despues_json={"estatus": getattr(doc, "estatus", None), "folio": getattr(doc, "folio", None),
                      "motivo": motivo, **(extra or {})},
        ip=ip_auditoria(request), user_agent=request.META.get("HTTP_USER_AGENT"),
    )


def auditar_evento(doc, request, accion, despues):
    AuditoriaEvento.objects.create(
        empresa_id=empresa_de(doc), usuario=_usuario(request.user), modulo="finanzas", accion=accion,
        tabla=doc._meta.db_table, id_registro=str(doc.pk),
        antes_json={"folio": getattr(doc, "folio", None)},
        despues_json={"folio": getattr(doc, "folio", None), **despues},
        ip=ip_auditoria(request), user_agent=request.META.get("HTTP_USER_AGENT"),
    )


def auditar_baja(doc, request, extra=None):
    """Borrado de un borrador (nunca movió saldos): queda quién, cuándo y qué."""
    data = request.data if hasattr(request.data, "get") else {}
    AuditoriaEvento.objects.create(
        empresa_id=empresa_de(doc), usuario=_usuario(request.user), modulo="finanzas", accion="BAJA",
        tabla=doc._meta.db_table, id_registro=str(doc.pk),
        antes_json={"estatus": getattr(doc, "estatus", None), "folio": getattr(doc, "folio", None), **(extra or {})},
        despues_json={"eliminado": True, "motivo": data.get("motivo") or request.query_params.get("motivo") or None},
        ip=ip_auditoria(request), user_agent=request.META.get("HTTP_USER_AGENT"),
    )
