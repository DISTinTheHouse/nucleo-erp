"""Timbrar el CFDI de una factura de venta con Facturama (``POST /facturas/{id}/timbrar/``).

Acción aparte de ``emitir``: no toca CxC, póliza ni estatus de la factura.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Cobro, Factura
from finanzas.services.facturama.acceso import empresa_usa_facturama
from finanzas.services.facturama.cfdi_payload import construir_payload_cfdi, construir_payload_rep
from finanzas.services.facturama.exceptions import FacturamaAPIException
from finanzas.services.facturama.service import FacturamaService


def _mensaje(exc):
    """``Message`` + ``ModelState`` de Facturama en una línea."""
    d = exc.details
    if isinstance(d, dict):
        partes = [str(d["Message"])] if d.get("Message") else []
        for campo, msgs in (d.get("ModelState") or {}).items():
            partes += [f"{campo}: {m}" for m in (msgs if isinstance(msgs, list) else [msgs])]
        if partes:
            return " ".join(partes)
    return str(d)[:500] if d else exc.message


def _sello(respuesta):
    """``(uuid, id, fecha)`` del timbre; ``uuid`` es ``None`` si no vino."""
    respuesta = respuesta if isinstance(respuesta, dict) else {}
    sello = (respuesta.get("Complement") or {}).get("TaxStamp") or {}
    fecha = parse_datetime(sello.get("Date") or "") or timezone.now()
    return sello.get("Uuid"), respuesta.get("Id"), timezone.make_aware(fecha) if timezone.is_naive(fecha) else fecha


def _timbrar(obj, payload, campos, clave, nombre):
    """Manda ``payload`` y guarda el timbre en ``obj`` (ya bloqueado). Regresa el error a levantar.

    ``campos`` = (uuid, facturama_id, fecha, pendiente). 4xx: Facturama rechazó, no hay
    CFDI y se puede reintentar. Timeout, red, 5xx o 2xx sin UUID: pudo timbrarse, así que
    se marca ``pendiente`` y no se reintenta hasta revisarlo (#544).
    """
    f_uuid, f_id, f_fecha, f_pendiente = campos
    try:
        respuesta = FacturamaService().create_cfdi_emision(payload)
    except FacturamaAPIException as exc:
        if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 408:
            return ErrorDeNegocio({clave: f"Facturama rechazó el {nombre}: {_mensaje(exc)}"})
        respuesta, detalle = None, _mensaje(exc)
    else:
        detalle = "respondió sin UUID"
    uuid, facturama_id, fecha = _sello(respuesta)
    if uuid:
        setattr(obj, f_uuid, uuid)
        setattr(obj, f_fecha, fecha)
    else:
        setattr(obj, f_pendiente, True)
    setattr(obj, f_id, facturama_id)
    obj.save(update_fields=[*campos, "updated_at"])
    if uuid:
        return None
    return ErrorDeNegocio({clave: (
        f"Facturama no confirmó el {nombre} ({detalle}). Pudo generarse: revísalo en Facturama "
        f"y, si no existe, desmarca '{f_pendiente}' en el admin antes de reintentar."
    )})


def _exigir_empresa_duena(empresa, clave):
    # Una sola cuenta de Facturama (un RFC emisor): otra empresa timbraría con ese RFC (#543).
    if not empresa_usa_facturama(empresa):
        raise ErrorDeNegocio({clave: "Solo la empresa dueña de la cuenta de Facturama puede timbrar."})


def _copiar(origen, destino, campos):
    for campo in campos:
        setattr(destino, campo, getattr(origen, campo))


CAMPOS_CFDI = ("cfdi_uuid", "facturama_id", "cfdi_fecha_timbrado", "cfdi_pendiente")
CAMPOS_REP = ("rep_uuid", "rep_facturama_id", "rep_fecha_timbrado", "rep_pendiente")
PENDIENTE = "Hay un timbrado sin confirmar: revísalo en Facturama y desmarca '{}' en el admin."


class FacturaTimbradoService:
    @staticmethod
    def timbrar(factura):
        error = FacturaTimbradoService._timbrar_factura(factura)
        if error:
            raise error
        return factura

    @staticmethod
    def timbrar_rep(cobro):
        """Timbra el complemento de pago (REP) de un cobro aplicado. No toca saldos ni pólizas."""
        error = FacturaTimbradoService._timbrar_rep(cobro)
        if error:
            raise error
        return cobro

    # Los ``_privados`` REGRESAN el error: lo que respondió Facturama se guarda, y
    # levantarlo dentro del ``atomic`` lo desharía.

    @staticmethod
    @transaction.atomic
    def _timbrar_factura(factura):
        bloqueada = (
            Factura.objects.select_for_update(of=("self",))
            .select_related("empresa", "pedido__cliente_regimen_fiscal", "cliente__sat_regimen_fiscal",
                            "cliente__sat_uso_cfdi", "sucursal", "moneda", "serie_folio")
            .get(pk=factura.pk)
        )
        _exigir_empresa_duena(bloqueada.empresa, "cfdi")
        if bloqueada.cfdi_uuid:
            raise ErrorDeNegocio({"cfdi": f"La factura ya está timbrada (UUID {bloqueada.cfdi_uuid})."})
        if bloqueada.cfdi_pendiente:
            raise ErrorDeNegocio({"cfdi": PENDIENTE.format("cfdi_pendiente")})
        if not bloqueada.activo or bloqueada.estatus == Factura.FacturaStatus.CANCELADA:
            raise ErrorDeNegocio({"estatus": "Solo se timbra una factura activa y no cancelada."})
        error = _timbrar(bloqueada, construir_payload_cfdi(bloqueada), CAMPOS_CFDI, "cfdi", "CFDI")
        _copiar(bloqueada, factura, CAMPOS_CFDI)
        return error

    @staticmethod
    @transaction.atomic
    def _timbrar_rep(cobro):
        bloqueado = Cobro.objects.select_for_update(of=("self",)).select_related("empresa").get(pk=cobro.pk)
        _exigir_empresa_duena(bloqueado.empresa, "rep")
        if bloqueado.rep_uuid:
            raise ErrorDeNegocio({"rep": f"El cobro ya tiene REP (UUID {bloqueado.rep_uuid})."})
        if bloqueado.rep_pendiente:
            raise ErrorDeNegocio({"rep": PENDIENTE.format("rep_pendiente")})
        if not bloqueado.activo or bloqueado.estatus != Cobro.Estatus.APLICADO:
            raise ErrorDeNegocio({"rep": "Solo se timbra el REP de un cobro activo y aplicado."})
        error = _timbrar(bloqueado, construir_payload_rep(bloqueado), CAMPOS_REP, "rep", "REP")
        _copiar(bloqueado, cobro, CAMPOS_REP)
        return error

    @staticmethod
    def cancelar_cfdi(factura):
        """Cancela el CFDI ante el SAT (motivo 02). Levanta ``ErrorDeNegocio`` si no queda cancelado."""
        try:
            respuesta = FacturamaService().cancel_cfdi(factura.facturama_id)
        except FacturamaAPIException as exc:
            raise ErrorDeNegocio({"cfdi": f"Facturama no canceló el CFDI: {_mensaje(exc)}"})
        estatus = str((respuesta or {}).get("Status") or "").lower()
        if estatus == "requested":
            raise ErrorDeNegocio({"cfdi": "El receptor debe aceptar la cancelación ante el SAT; vuelve a cancelar cuando la acepte."})
        if estatus != "canceled":
            raise ErrorDeNegocio({"cfdi": f"El SAT no canceló el CFDI (estatus: {estatus or 'vacío'})."})
