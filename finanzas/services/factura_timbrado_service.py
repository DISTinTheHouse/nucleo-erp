"""Timbrar el CFDI de una factura de venta con Facturama (``POST /facturas/{id}/timbrar/``).

Acción aparte de ``emitir``: no toca CxC, póliza ni estatus de la factura.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Factura
from finanzas.services.facturama.cfdi_payload import construir_payload_cfdi
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


class FacturaTimbradoService:
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

        sello = ((respuesta or {}).get("Complement") or {}).get("TaxStamp") or {}
        if not sello.get("Uuid"):
            raise ErrorDeNegocio({"cfdi": f"Facturama respondió sin UUID (Id {respuesta.get('Id')})."})
        fecha = parse_datetime(sello.get("Date") or "") or timezone.now()
        bloqueada.cfdi_uuid = sello["Uuid"]
        bloqueada.facturama_id = respuesta.get("Id")
        bloqueada.cfdi_fecha_timbrado = timezone.make_aware(fecha) if timezone.is_naive(fecha) else fecha
        bloqueada.save(update_fields=["cfdi_uuid", "facturama_id", "cfdi_fecha_timbrado", "updated_at"])
        factura.cfdi_uuid, factura.facturama_id = bloqueada.cfdi_uuid, bloqueada.facturama_id
        factura.cfdi_fecha_timbrado = bloqueada.cfdi_fecha_timbrado
        return factura
