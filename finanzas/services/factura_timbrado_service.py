"""Timbrar el CFDI de una factura de venta con Facturama (``POST /facturas/{id}/timbrar/``).

Acción aparte de ``emitir``: no toca CxC, póliza ni estatus de la factura.
"""

import base64

from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Cobro, Factura, NotaCredito
from finanzas.services.nota_credito_service import NotaCreditoService
from finanzas.services.facturama.acceso import empresa_usa_facturama
from finanzas.services.facturama.cfdi_payload import construir_payload_cfdi, construir_payload_nota_credito, construir_payload_rep
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


def _timbrar(modelo, pk, preparar, campos, clave, nombre):
    """Timbra con reserva previa: ``pendiente`` se confirma ANTES de llamar a Facturama (#601, #600).

    1) ``atomic`` corto: bloquea, ``preparar(obj)`` valida y arma el payload, y se guarda
       ``pendiente=True``. 2) Facturama, fuera de toda transacción. 3) ``atomic`` corto: guarda
       el timbre. Si la plataforma mata el proceso en (2), la reserva ya está en la DB y un
       reintento queda bloqueado hasta revisar Facturama. Regresa ``(obj, error)``.

    ``campos`` = (uuid, facturama_id, fecha, pendiente). 4xx: Facturama rechazó, no hay CFDI,
    se libera la reserva y se puede reintentar. Timeout, red, 5xx o 2xx sin UUID: pudo
    timbrarse, así que sigue ``pendiente`` (#544).
    """
    f_uuid, f_id, f_fecha, f_pendiente = campos
    with transaction.atomic():
        obj = modelo.objects.select_for_update(of=("self",)).get(pk=pk)
        payload = preparar(obj)
        setattr(obj, f_pendiente, True)
        obj.save(update_fields=[f_pendiente, "updated_at"])
    rechazo = None
    try:
        respuesta = FacturamaService().create_cfdi_emision(payload)
    except FacturamaAPIException as exc:
        if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 408:
            rechazo = ErrorDeNegocio({clave: f"Facturama rechazó el {nombre}: {_mensaje(exc)}"})
        respuesta, detalle = None, _mensaje(exc)
    else:
        detalle = "respondió sin UUID"
    uuid, facturama_id, fecha = _sello(respuesta)
    with transaction.atomic():
        obj = modelo.objects.select_for_update(of=("self",)).get(pk=pk)
        if uuid:
            setattr(obj, f_uuid, uuid)
            setattr(obj, f_fecha, fecha)
            setattr(obj, f_pendiente, False)
        elif rechazo:
            setattr(obj, f_pendiente, False)
        setattr(obj, f_id, facturama_id)
        obj.save(update_fields=[*campos, "updated_at"])
    if uuid:
        return obj, None
    return obj, rechazo or ErrorDeNegocio({clave: (
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
# Facturama documenta ``pending``; ``requested`` es el valor que el ERP ya manejaba (#630).
CANCELACION_PENDIENTE = ("pending", "requested")
MSG_CANCELACION_PENDIENTE = "El receptor debe aceptar la cancelación ante el SAT; vuelve a cancelar cuando la acepte."
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

    @staticmethod
    def cancelar_rep(cobro):
        """Cancela el REP ante el SAT (motivo 02). Facturama va fuera de toda transacción (#627).

        ``canceled``: el UUID pasa a ``rep_cancelado_uuid`` (#625). ``pending``/``requested``:
        queda ``rep_cancelacion_solicitada`` (#624) y se reintenta cuando el receptor acepte.
        Se cancela con ``rep_facturama_id`` aunque falte el UUID (timbrado sin confirmar, #622).
        """
        with transaction.atomic():
            bloqueado = Cobro.objects.select_for_update(of=("self",)).select_related("empresa").get(pk=cobro.pk)
            _exigir_empresa_duena(bloqueado.empresa, "rep")
            if not bloqueado.rep_facturama_id:
                raise ErrorDeNegocio({"rep": "El cobro no tiene un REP en Facturama que cancelar."})
        error = None
        try:
            respuesta = FacturamaService().cancel_cfdi(bloqueado.rep_facturama_id)
        except FacturamaAPIException as exc:
            if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 408:
                raise ErrorDeNegocio({"rep": f"Facturama no canceló el REP: {_mensaje(exc)}"})
            # #623: sin respuesta no se sabe si se canceló.
            raise ErrorDeNegocio({"rep_incierto": f"Facturama no respondió ({_mensaje(exc)}); no se sabe si se canceló. Vuelve a intentar."})
        estatus = str((respuesta or {}).get("Status") or "").lower()
        with transaction.atomic():
            bloqueado = Cobro.objects.select_for_update(of=("self",)).get(pk=cobro.pk)
            if estatus == "canceled":
                bloqueado.rep_cancelado_uuid = bloqueado.rep_uuid or bloqueado.rep_cancelado_uuid
                bloqueado.rep_fecha_cancelacion = timezone.now()
                bloqueado.rep_uuid = bloqueado.rep_facturama_id = bloqueado.rep_fecha_timbrado = None
                bloqueado.rep_pendiente = bloqueado.rep_cancelacion_solicitada = False
            elif estatus in CANCELACION_PENDIENTE:
                bloqueado.rep_cancelacion_solicitada = True
                error = ErrorDeNegocio({"cancelacion_pendiente": MSG_CANCELACION_PENDIENTE})
            else:
                bloqueado.rep_cancelacion_solicitada = False
                error = ErrorDeNegocio({"rep": f"El SAT no canceló el REP (estatus: {estatus or 'vacío'})."})
            bloqueado.save(update_fields=[*CAMPOS_REP, "rep_cancelado_uuid", "rep_fecha_cancelacion", "rep_cancelacion_solicitada", "updated_at"])
        _copiar(bloqueado, cobro, [*CAMPOS_REP, "rep_cancelado_uuid", "rep_fecha_cancelacion", "rep_cancelacion_solicitada"])
        if error:
            raise error
        return cobro

    # Los ``_privados`` REGRESAN el error: lo que respondió Facturama se guarda, y
    # levantarlo dentro del ``atomic`` lo desharía.

    @staticmethod
    def _timbrar_factura(factura):
        def preparar(f):
            f = Factura.objects.select_related(
                "empresa", "pedido__cliente_regimen_fiscal", "cliente__sat_regimen_fiscal",
                "cliente__sat_uso_cfdi", "sucursal", "moneda", "serie_folio",
            ).get(pk=f.pk)
            _exigir_empresa_duena(f.empresa, "cfdi")
            if f.cfdi_uuid:
                raise ErrorDeNegocio({"cfdi": f"La factura ya está timbrada (UUID {f.cfdi_uuid})."})
            if f.cfdi_pendiente:
                raise ErrorDeNegocio({"cfdi": PENDIENTE.format("cfdi_pendiente")})
            if not f.activo or f.estatus == Factura.FacturaStatus.CANCELADA:
                raise ErrorDeNegocio({"estatus": "Solo se timbra una factura activa y no cancelada."})
            return construir_payload_cfdi(f)

        obj, error = _timbrar(Factura, factura.pk, preparar, CAMPOS_CFDI, "cfdi", "CFDI")
        _copiar(obj, factura, CAMPOS_CFDI)
        return error

    @staticmethod
    def _timbrar_rep(cobro):
        def preparar(c):
            _exigir_empresa_duena(Cobro.objects.select_related("empresa").get(pk=c.pk).empresa, "rep")
            if c.rep_uuid:
                raise ErrorDeNegocio({"rep": f"El cobro ya tiene REP (UUID {c.rep_uuid})."})
            if c.rep_pendiente:
                raise ErrorDeNegocio({"rep": PENDIENTE.format("rep_pendiente")})
            if not c.activo or c.estatus != Cobro.Estatus.APLICADO:
                raise ErrorDeNegocio({"rep": "Solo se timbra el REP de un cobro activo y aplicado."})
            return construir_payload_rep(c)

        obj, error = _timbrar(Cobro, cobro.pk, preparar, CAMPOS_REP, "rep", "REP")
        _copiar(obj, cobro, CAMPOS_REP)
        return error

    @staticmethod
    def enviar_cfdi(factura, email):
        """Manda por correo el PDF y el XML oficiales del CFDI (descargados de Facturama)."""
        if not factura.facturama_id:
            raise ErrorDeNegocio({"cfdi": "La factura no tiene CFDI timbrado."})
        if factura.cfdi_cancelado:
            raise ErrorDeNegocio({"cfdi": "El CFDI está cancelado ante el SAT; no se envía."})
        try:
            validate_email(email or "")
        except ValidationError:
            raise ErrorDeNegocio({"email": "Correo inválido o vacío."})
        servicio = FacturamaService()
        nombre = factura.folio or str(factura.pk)
        correo = EmailMessage(
            subject=f"Factura {nombre}",
            body=f"Adjuntamos el CFDI de la factura {nombre} (UUID {factura.cfdi_uuid}).",
            to=[email],
        )
        try:
            for formato, tipo in (("pdf", "application/pdf"), ("xml", "application/xml")):
                archivo = servicio.get_cfdi_file(formato, "issued", factura.facturama_id) or {}
                correo.attach(f"{nombre}.{formato}", base64.b64decode(archivo.get("Content") or ""), tipo)
        except FacturamaAPIException as exc:
            raise ErrorDeNegocio({"cfdi": f"Facturama no entregó el archivo: {_mensaje(exc)}"})
        correo.send()
        return email

    @staticmethod
    def timbrar_nota_credito(nota):
        """Timbra el CFDI de egreso de una nota de crédito emitida. No toca saldos ni pólizas."""
        error = FacturaTimbradoService._timbrar_nota(nota)
        if error:
            raise error
        return nota

    @staticmethod
    def _timbrar_nota(nota):
        def preparar(n):
            n = NotaCredito.objects.select_related(
                "factura__empresa", "factura__pedido__cliente_regimen_fiscal",
                "factura__cliente__sat_regimen_fiscal", "factura__sucursal",
            ).get(pk=n.pk)
            _exigir_empresa_duena(n.factura.empresa, "cfdi")
            if n.cfdi_uuid:
                raise ErrorDeNegocio({"cfdi": f"La nota ya está timbrada (UUID {n.cfdi_uuid})."})
            if n.cfdi_pendiente:
                raise ErrorDeNegocio({"cfdi": PENDIENTE.format("cfdi_pendiente")})
            if n.estatus != NotaCredito.Estatus.EMITIDA:
                raise ErrorDeNegocio({"estatus": "Solo se timbra una nota de crédito Emitida."})
            NotaCreditoService.validar_factura(n.factura)
            return construir_payload_nota_credito(n)

        obj, error = _timbrar(NotaCredito, nota.pk, preparar, CAMPOS_CFDI, "cfdi", "CFDI")
        _copiar(obj, nota, CAMPOS_CFDI)
        return error

    @staticmethod
    def cancelar_cfdi(factura):
        """Cancela el CFDI ante el SAT (motivo 02) y guarda el resultado. Levanta si no quedó cancelado."""
        error = FacturaTimbradoService._cancelar_cfdi(factura)
        if error:
            raise error

    @staticmethod
    @transaction.atomic
    def _cancelar_cfdi(factura):
        bloqueada = Factura.objects.select_for_update(of=("self",)).select_related("empresa").get(pk=factura.pk)
        if bloqueada.cfdi_cancelado:
            return None
        _exigir_empresa_duena(bloqueada.empresa, "cfdi")  # #564
        try:
            respuesta = FacturamaService().cancel_cfdi(bloqueada.facturama_id)
        except FacturamaAPIException as exc:
            if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 408:
                return ErrorDeNegocio({"cfdi": f"Facturama no canceló el CFDI: {_mensaje(exc)}"})
            # #563: sin respuesta no se sabe si se canceló; reintentar es seguro.
            return ErrorDeNegocio({"cfdi": f"Facturama no respondió ({_mensaje(exc)}); no se sabe si se canceló. Vuelve a intentar."})
        estatus = str((respuesta or {}).get("Status") or "").lower()
        if estatus == "canceled":
            bloqueada.cfdi_cancelado, bloqueada.cfdi_cancelacion_solicitada = True, False
        elif estatus in CANCELACION_PENDIENTE:  # #565, #630
            bloqueada.cfdi_cancelacion_solicitada = True
        else:
            # Rechazo del receptor (``active``): ya no hay solicitud en curso (#633).
            bloqueada.cfdi_cancelacion_solicitada = False
        bloqueada.save(update_fields=["cfdi_cancelado", "cfdi_cancelacion_solicitada", "updated_at"])
        factura.cfdi_cancelado = bloqueada.cfdi_cancelado
        factura.cfdi_cancelacion_solicitada = bloqueada.cfdi_cancelacion_solicitada
        if estatus in CANCELACION_PENDIENTE:
            return ErrorDeNegocio({"cancelacion_pendiente": MSG_CANCELACION_PENDIENTE})
        if not bloqueada.cfdi_cancelado:
            return ErrorDeNegocio({"cfdi": f"El SAT no canceló el CFDI (estatus: {estatus or 'vacío'})."})
        return None
