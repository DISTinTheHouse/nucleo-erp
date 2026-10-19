"""Timbrado y cancelación del CFDI de una factura de venta con Facturama.

Solo timbra la empresa dueña de la cuenta única (``FACTURAMA_EMPRESA_CODIGO``) y
solo con ``FACTURAMA_TIMBRAR_AL_EMITIR`` encendido: las demás empresas emiten
como siempre, sin CFDI. Cancelar, en cambio, no depende del flag: una factura
timbrada nunca se cancela en el ERP sin cancelar antes su CFDI ante el SAT.

Lo que responde Facturama se guarda en su propia transacción ANTES de la parte
contable: el CFDI ya existe ante el SAT y no se deshace con un rollback, así que
perder su UUID por un fallo posterior sería peor que una factura timbrada en
Borrador (reintentar ``emitir`` no la vuelve a timbrar).
"""

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Factura
from finanzas.services.facturama.acceso import empresa_usa_facturama
from finanzas.services.facturama.cfdi_payload import construir_payload_cfdi
from finanzas.services.facturama.exceptions import FacturamaAPIException
from finanzas.services.facturama.service import FacturamaService

Cfdi = Factura.CfdiEstatus

CAMPOS_CFDI = (
    "cfdi_estatus", "cfdi_uuid", "facturama_id", "cfdi_fecha_timbrado", "cfdi_error",
    "cfdi_uso", "cfdi_forma_pago", "cfdi_metodo_pago",
)

# 02 = comprobante emitido con errores sin relación. El 01 exige el UUID que lo
# sustituye y hoy no hay flujo de sustitución.
MOTIVO_CANCELACION = "02"


def _mensaje_facturama(exc):
    """``Message`` + ``ModelState`` del error de Facturama, en una línea."""
    details = exc.details
    if isinstance(details, dict):
        partes = [str(details["Message"])] if details.get("Message") else []
        for campo, mensajes in (details.get("ModelState") or {}).items():
            for mensaje in mensajes if isinstance(mensajes, list) else [mensajes]:
                partes.append(f"{campo}: {mensaje}" if campo else str(mensaje))
        if partes:
            return " ".join(partes)
    if isinstance(details, str) and details.strip():
        return details.strip()[:500]
    return exc.message


def _rechazo_definitivo(exc):
    """4xx: Facturama revisó y no timbró. Timeout, conexión o 5xx: no se sabe."""
    return exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code != 408


def _fecha_timbrado(valor):
    fecha = parse_datetime(valor) if isinstance(valor, str) else None
    if fecha is None:
        return timezone.now()
    return timezone.make_aware(fecha) if timezone.is_naive(fecha) else fecha


def _copiar(origen, destino):
    for campo in CAMPOS_CFDI:
        setattr(destino, campo, getattr(origen, campo))


def _bloquear(factura):
    # ``of=self``: Postgres no deja ``FOR UPDATE`` del lado nulo de un outer join.
    return (
        Factura.objects.select_for_update(of=("self",))
        .select_related(
            "pedido__cliente_regimen_fiscal", "cliente__sat_regimen_fiscal", "cliente__sat_uso_cfdi",
            "sucursal", "moneda", "serie_folio",
        )
        .get(pk=factura.pk)
    )


class FacturaTimbradoService:
    @staticmethod
    def aplica(factura):
        return bool(getattr(settings, "FACTURAMA_TIMBRAR_AL_EMITIR", False)) and empresa_usa_facturama(
            factura.empresa
        )

    @staticmethod
    def timbrar(factura):
        """Timbra la factura si aún no lo está. Idempotente: una timbrada no se reenvía."""
        error = FacturaTimbradoService._timbrar(factura)
        if error is not None:
            raise error

    @staticmethod
    def cancelar_cfdi(factura):
        """Cancela el CFDI ante el SAT si la factura está timbrada; si no, no hace nada.

        Levanta ``ErrorDeNegocio`` si el CFDI no quedó cancelado: quien llama no
        debe cancelar la factura en el ERP.
        """
        error = FacturaTimbradoService._cancelar(factura)
        if error is not None:
            raise error

    # Los ``_privados`` REGRESAN el error en vez de levantarlo: lo que respondió
    # Facturama tiene que guardarse, y levantar dentro del ``atomic`` lo desharía.

    @staticmethod
    @transaction.atomic
    def _timbrar(factura):
        bloqueada = _bloquear(factura)
        estatus = bloqueada.cfdi_estatus
        if estatus == Cfdi.TIMBRADA:
            _copiar(bloqueada, factura)
            return None
        if estatus == Cfdi.PENDIENTE:
            return ErrorDeNegocio({"cfdi": (
                f"El timbrado anterior de la factura {bloqueada.folio} quedó sin confirmar. "
                "Revisa en Facturama si se generó y corrige el estatus CFDI en el admin antes de reintentar."
            )})
        if estatus in (Cfdi.CANCELACION_SOLICITADA, Cfdi.CANCELADA):
            return ErrorDeNegocio({"cfdi": f"El CFDI de esta factura está {estatus}; no se vuelve a timbrar."})
        if not bloqueada.activo or bloqueada.estatus != Factura.FacturaStatus.BORRADOR:
            return ErrorDeNegocio({"estatus": "Solo se timbra una factura activa en Borrador."})

        payload, datos = construir_payload_cfdi(bloqueada)
        try:
            respuesta = FacturamaService().create_cfdi_emision(payload)
        except FacturamaAPIException as exc:
            if _rechazo_definitivo(exc):
                bloqueada.cfdi_estatus = Cfdi.ERROR
                bloqueada.cfdi_error = f"Facturama rechazó el CFDI: {_mensaje_facturama(exc)}"
            else:
                bloqueada.cfdi_estatus = Cfdi.PENDIENTE
                bloqueada.cfdi_error = (
                    f"Facturama no confirmó el timbrado ({_mensaje_facturama(exc)}). Pudo generarse: "
                    f"revisa en Facturama el folio {bloqueada.folio} antes de reintentar."
                )
            bloqueada.save(update_fields=["cfdi_estatus", "cfdi_error", "updated_at"])
            _copiar(bloqueada, factura)
            return ErrorDeNegocio({"cfdi": bloqueada.cfdi_error})

        respuesta = respuesta if isinstance(respuesta, dict) else {}
        sello = (respuesta.get("Complement") or {}).get("TaxStamp") or {}
        bloqueada.facturama_id = respuesta.get("Id") or None
        bloqueada.cfdi_uuid = sello.get("Uuid") or None
        if bloqueada.cfdi_uuid:
            bloqueada.cfdi_estatus = Cfdi.TIMBRADA
            bloqueada.cfdi_fecha_timbrado = _fecha_timbrado(sello.get("Date"))
            bloqueada.cfdi_error = None
            for campo, valor in datos.items():
                setattr(bloqueada, campo, valor)
        else:
            bloqueada.cfdi_estatus = Cfdi.PENDIENTE
            bloqueada.cfdi_error = (
                f"Facturama respondió sin UUID (Id {bloqueada.facturama_id or 'desconocido'}). "
                "Revisa el comprobante en Facturama."
            )
        bloqueada.save(update_fields=[*CAMPOS_CFDI, "updated_at"])
        _copiar(bloqueada, factura)
        return None if bloqueada.cfdi_uuid else ErrorDeNegocio({"cfdi": bloqueada.cfdi_error})

    @staticmethod
    @transaction.atomic
    def _cancelar(factura):
        bloqueada = _bloquear(factura)
        estatus = bloqueada.cfdi_estatus
        if estatus == Cfdi.PENDIENTE:
            return ErrorDeNegocio({"cfdi": (
                "La factura tiene un timbrado sin confirmar: resuélvelo en Facturama y en el admin antes de cancelarla."
            )})
        if estatus not in (Cfdi.TIMBRADA, Cfdi.CANCELACION_SOLICITADA):
            return None
        if not bloqueada.facturama_id:
            return ErrorDeNegocio({"cfdi": (
                "La factura está timbrada pero no tiene el Id de Facturama: cancela el CFDI en Facturama "
                "y corrige el estatus CFDI en el admin."
            )})

        try:
            respuesta = FacturamaService().cancel_cfdi(bloqueada.facturama_id, motive=MOTIVO_CANCELACION)
        except FacturamaAPIException as exc:
            return ErrorDeNegocio({"cfdi": f"Facturama no canceló el CFDI: {_mensaje_facturama(exc)}"})

        resultado = str(respuesta.get("Status") or "").lower() if isinstance(respuesta, dict) else ""
        error = None
        if resultado == "canceled":
            bloqueada.cfdi_estatus = Cfdi.CANCELADA
            bloqueada.cfdi_error = None
        elif resultado == "requested":
            bloqueada.cfdi_estatus = Cfdi.CANCELACION_SOLICITADA
            bloqueada.cfdi_error = None
            error = ErrorDeNegocio({"cfdi": (
                "El SAT pidió al receptor aceptar la cancelación del CFDI. La factura sigue vigente "
                "hasta que la acepte: vuelve a cancelarla después."
            )})
        elif resultado == "rejected":
            bloqueada.cfdi_estatus = Cfdi.TIMBRADA
            bloqueada.cfdi_error = "El receptor rechazó la cancelación del CFDI."
            error = ErrorDeNegocio({"cfdi": bloqueada.cfdi_error})
        else:
            return ErrorDeNegocio({"cfdi": f"Facturama respondió un estatus de cancelación desconocido: {resultado or 'vacío'}."})

        bloqueada.save(update_fields=["cfdi_estatus", "cfdi_error", "updated_at"])
        _copiar(bloqueada, factura)
        return error
