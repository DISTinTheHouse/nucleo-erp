"""Timbrar el CFDI de una factura de venta con Facturama (``POST /facturas/{id}/timbrar/``).

Acción aparte de ``emitir``: no toca CxC, póliza ni estatus de la factura.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Cobro, Factura
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
    sello = ((respuesta or {}).get("Complement") or {}).get("TaxStamp") or {}
    if not sello.get("Uuid"):
        raise ErrorDeNegocio({"cfdi": f"Facturama respondió sin UUID (Id {(respuesta or {}).get('Id')})."})
    fecha = parse_datetime(sello.get("Date") or "") or timezone.now()
    return sello["Uuid"], respuesta.get("Id"), timezone.make_aware(fecha) if timezone.is_naive(fecha) else fecha


class FacturaTimbradoService:
    @staticmethod
    @transaction.atomic
    def timbrar_rep(cobro):
        """Timbra el complemento de pago (REP) de un cobro aplicado. No toca saldos ni pólizas."""
        bloqueado = Cobro.objects.select_for_update().get(pk=cobro.pk)
        if bloqueado.rep_uuid:
            raise ErrorDeNegocio({"rep": f"El cobro ya tiene REP (UUID {bloqueado.rep_uuid})."})
        if not bloqueado.activo or bloqueado.estatus != Cobro.Estatus.APLICADO:
            raise ErrorDeNegocio({"rep": "Solo se timbra el REP de un cobro activo y aplicado."})
        payload = construir_payload_rep(bloqueado)
        try:
            respuesta = FacturamaService().create_cfdi_emision(payload)
        except FacturamaAPIException as exc:
            raise ErrorDeNegocio({"rep": f"Facturama rechazó el REP: {_mensaje(exc)}"})
        bloqueado.rep_uuid, bloqueado.rep_facturama_id, bloqueado.rep_fecha_timbrado = _sello(respuesta)
        bloqueado.save(update_fields=["rep_uuid", "rep_facturama_id", "rep_fecha_timbrado", "updated_at"])
        cobro.rep_uuid, cobro.rep_facturama_id = bloqueado.rep_uuid, bloqueado.rep_facturama_id
        cobro.rep_fecha_timbrado = bloqueado.rep_fecha_timbrado
        return cobro

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

    @staticmethod
    @transaction.atomic
    def timbrar(factura):
        bloqueada = (
            Factura.objects.select_for_update(of=("self",))
            .select_related("pedido__cliente_regimen_fiscal", "cliente__sat_regimen_fiscal",
                            "cliente__sat_uso_cfdi", "sucursal", "moneda", "serie_folio")
            .get(pk=factura.pk)
        )
        if bloqueada.cfdi_uuid:
            raise ErrorDeNegocio({"cfdi": f"La factura ya está timbrada (UUID {bloqueada.cfdi_uuid})."})
        if not bloqueada.activo or bloqueada.estatus == Factura.FacturaStatus.CANCELADA:
            raise ErrorDeNegocio({"estatus": "Solo se timbra una factura activa y no cancelada."})

        payload = construir_payload_cfdi(bloqueada)
        try:
            respuesta = FacturamaService().create_cfdi_emision(payload)
        except FacturamaAPIException as exc:
            raise ErrorDeNegocio({"cfdi": f"Facturama rechazó el CFDI: {_mensaje(exc)}"})

        bloqueada.cfdi_uuid, bloqueada.facturama_id, bloqueada.cfdi_fecha_timbrado = _sello(respuesta)
        bloqueada.save(update_fields=["cfdi_uuid", "facturama_id", "cfdi_fecha_timbrado", "updated_at"])
        factura.cfdi_uuid, factura.facturama_id = bloqueada.cfdi_uuid, bloqueada.facturama_id
        factura.cfdi_fecha_timbrado = bloqueada.cfdi_fecha_timbrado
        return factura
