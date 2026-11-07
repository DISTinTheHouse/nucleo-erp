"""Timbrar el CFDI de una factura de venta con Facturama (``POST /facturas/{id}/timbrar/``).

Acción aparte de ``emitir``: no toca CxC, póliza ni estatus de la factura.
"""

import base64
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from smtplib import SMTPException

from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.conf import settings
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Cobro, Factura, MotivoCancelacionSat, NotaCredito
from finanzas.services.nota_credito_service import NotaCreditoService
from finanzas.services.facturama.acceso import empresa_usa_facturama
from finanzas.services.facturama.cfdi_payload import _receptor, construir_payload_cfdi, construir_payload_nota_credito, construir_payload_rep
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
        reservado = [f_pendiente, "updated_at"]
        if hasattr(obj, f"{f_pendiente}_desde"):  # facturas y notas (#901)
            setattr(obj, f"{f_pendiente}_desde", timezone.now())
            reservado.append(f"{f_pendiente}_desde")
        obj.save(update_fields=reservado)
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
    siguiente = "usa conciliar-cfdi/ para traer el timbre o liberarlo" if clave == "cfdi" else (
        f"revísalo en Facturama y, si no existe, desmarca '{f_pendiente}' en el admin"
    )
    return obj, rechazo or ErrorDeNegocio({clave: (
        f"Facturama no confirmó el {nombre} ({detalle}). Pudo generarse: {siguiente} antes de reintentar."
    )})


def _exigir_empresa_duena(empresa, clave):
    # Una sola cuenta de Facturama (un RFC emisor): otra empresa timbraría con ese RFC (#543).
    if not empresa_usa_facturama(empresa):
        raise ErrorDeNegocio({clave: "Solo la empresa dueña de la cuenta de Facturama puede timbrar."})


def normalizar_motivo_sat(valor):
    """``motivo_sat`` del body: ``""`` si no vino; si vino, debe ser 02 o 03 (EC-806)."""
    motivo = "" if valor is None else str(valor).strip()
    if motivo and motivo not in MotivoCancelacionSat.values:
        raise ErrorDeNegocio({"motivo_sat": f"Motivo SAT no válido ({motivo[:10]}): usa {MOTIVOS_SAT_TEXTO}."})
    return motivo


def _motivo_sat(recibido, guardado, solicitada):
    """Motivo con que se pide la cancelación ante el SAT; lo elige el usuario, sin default (EC-806).

    Con la cancelación ya solicitada (falta que el receptor acepte), el reintento reenvía el
    motivo de la primera solicitud: si no viene se reusa, y uno distinto es 400.
    """
    recibido = normalizar_motivo_sat(recibido)
    if solicitada and guardado:
        if recibido and recibido != guardado:
            raise ErrorDeNegocio({"motivo_sat": (
                f"La cancelación ya se solicitó ante el SAT con motivo {guardado} "
                f"({MotivoCancelacionSat(guardado).label}); reintenta con el mismo motivo."
            )})
        return guardado
    if not recibido:
        raise ErrorDeNegocio({"motivo_sat": f"Elige el motivo de cancelación ante el SAT: {MOTIVOS_SAT_TEXTO}."})
    return recibido


def _copiar(origen, destino, campos):
    for campo in campos:
        setattr(destino, campo, getattr(origen, campo))


CAMPOS_CFDI = ("cfdi_uuid", "facturama_id", "cfdi_fecha_timbrado", "cfdi_pendiente")
CAMPOS_REP = ("rep_uuid", "rep_facturama_id", "rep_fecha_timbrado", "rep_pendiente")
CAMPOS_REP_CANCELACION = ("rep_cancelado_uuid", "rep_fecha_cancelacion", "rep_cancelacion_solicitada", "rep_cancelacion_motivo")
CAMPOS_CFDI_CANCELACION = ("cfdi_cancelado", "cfdi_cancelacion_solicitada", "cfdi_cancelacion_motivo")
# Facturama documenta ``pending``; ``requested`` es el valor que el ERP ya manejaba (#630).
CANCELACION_PENDIENTE = ("pending", "requested")
MSG_CANCELACION_PENDIENTE = "El receptor debe aceptar la cancelación ante el SAT; vuelve a cancelar cuando la acepte."
PENDIENTE = "Hay un timbrado sin confirmar: revísalo en Facturama y desmarca '{}' en el admin."
PENDIENTE_CFDI = "Hay un timbrado sin confirmar: usa conciliar-cfdi/ para traer el timbre de Facturama o liberarlo."
MOTIVOS_SAT_TEXTO = " o ".join(f"{m.value} ({m.label})" for m in MotivoCancelacionSat)
# Páginas de ``GET /cfdi`` (10 por página) que se revisan al buscar un timbre sin confirmar.
PAGINAS_CONCILIAR = 30


def _es_candidato(c, tipo, folio, total):
    """El CFDI ``c`` del listado de Facturama es de este documento. Un registro mal formado
    (sin ``Id``/``Uuid`` o con ``Total`` no numérico) no se compara: nunca un 500 (#903)."""
    if not isinstance(c, dict) or not c.get("Uuid") or not c.get("Id"):
        return False
    if c.get("CfdiType") != tipo or str(c.get("Folio") or "").strip() != folio:
        return False
    if str(c.get("Status") or "").lower() != "active":
        return False
    try:
        return abs(Decimal(str(c.get("Total") or 0)) - total) <= Decimal("0.01")
    except InvalidOperation:
        return False


def _buscar_cfdi(obj, tipo, factura):
    """``(uuid, id, fecha)`` del CFDI de ``obj`` en Facturama, o ``None`` si no existe (#758).

    Primero por ``facturama_id``; si no hay, por RFC receptor + fecha, filtrando aquí por
    tipo, folio y total (Facturama ignora ``folioStart``/``keyword``). Levanta si
    Facturama no responde o si hay más de un candidato: no se adivina.
    """
    servicio = FacturamaService()
    if obj.facturama_id:
        try:
            uuid, facturama_id, fecha = _sello(servicio.get_cfdi(obj.facturama_id))
        except FacturamaAPIException as exc:
            if not (exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 408):
                raise ErrorDeNegocio({"cfdi": f"Facturama no respondió ({_mensaje(exc)}). Vuelve a intentar."})
        else:
            if uuid:
                return uuid, facturama_id or obj.facturama_id, fecha
    folio = (obj.folio or "").strip()
    if not folio:
        raise ErrorDeNegocio({"cfdi": "Sin folio no se puede buscar el CFDI en Facturama; revísalo ahí."})
    inicio = timezone.localdate(obj.created_at or timezone.now()) - timedelta(days=1)
    fin = timezone.localdate() + timedelta(days=1)
    params = {"rfc": _receptor(factura, {})["Rfc"], "dateStart": inicio.strftime("%d/%m/%Y"),
              "dateEnd": fin.strftime("%d/%m/%Y")}
    candidatos = []
    try:
        for pagina in range(PAGINAS_CONCILIAR):
            lote = servicio.list_cfdis(**params, page=pagina) or []
            if not isinstance(lote, list):  # #903
                raise ErrorDeNegocio({"cfdi": "Facturama devolvió datos inesperados; revísalo ahí."})
            candidatos += [c for c in lote if _es_candidato(c, tipo, folio, obj.total)]
            if len(lote) < 10:
                break
        else:
            # Se agotaron las páginas sin terminar el listado: no se puede afirmar que no existe (#900).
            raise ErrorDeNegocio({"cfdi": (
                f"No se pudo confirmar en Facturama: el cliente tiene más de {PAGINAS_CONCILIAR * 10} CFDIs "
                "en el periodo. Revísalo ahí."
            )})
    except FacturamaAPIException as exc:
        raise ErrorDeNegocio({"cfdi": f"Facturama no respondió ({_mensaje(exc)}). Vuelve a intentar."})
    # Un UUID que ya es de otro documento del ERP no es de éste.
    usados = {*Factura.objects.filter(cfdi_uuid__in=[c["Uuid"] for c in candidatos]).values_list("cfdi_uuid", flat=True),
              *NotaCredito.objects.filter(cfdi_uuid__in=[c["Uuid"] for c in candidatos]).values_list("cfdi_uuid", flat=True)}
    candidatos = [c for c in candidatos if c["Uuid"] not in usados]
    if len(candidatos) > 1:
        raise ErrorDeNegocio({"cfdi": f"Hay {len(candidatos)} CFDIs en Facturama con el folio {folio} y el mismo total; revísalo ahí."})
    if not candidatos:
        return None
    fecha = parse_datetime(candidatos[0].get("Date") or "") or timezone.now()
    return candidatos[0]["Uuid"], candidatos[0]["Id"], timezone.make_aware(fecha) if timezone.is_naive(fecha) else fecha


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
    def cancelar_rep(cobro, motivo_sat=None):
        """Cancela el REP ante el SAT con el motivo elegido (EC-806). Facturama va fuera de toda transacción (#627).

        ``canceled``: el UUID pasa a ``rep_cancelado_uuid`` (#625). ``pending``/``requested``:
        queda ``rep_cancelacion_solicitada`` (#624) y se reintenta cuando el receptor acepte.
        Se cancela con ``rep_facturama_id`` aunque falte el UUID (timbrado sin confirmar, #622).
        """
        # Validar es una lectura simple: un bloqueo aquí se soltaba antes de llamar a Facturama.
        actual = Cobro.objects.select_related("empresa").get(pk=cobro.pk)
        _exigir_empresa_duena(actual.empresa, "rep")
        if not actual.rep_facturama_id:
            raise ErrorDeNegocio({"rep": "El cobro no tiene un REP en Facturama que cancelar."})
        motivo = _motivo_sat(motivo_sat, actual.rep_cancelacion_motivo, actual.rep_cancelacion_solicitada)
        error = None
        try:
            respuesta = FacturamaService().cancel_cfdi(actual.rep_facturama_id, motivo)
        except FacturamaAPIException as exc:
            if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 408:
                raise ErrorDeNegocio({"rep": f"Facturama no canceló el REP: {_mensaje(exc)}"})
            # #623: sin respuesta no se sabe si se canceló.
            raise ErrorDeNegocio({"rep_incierto": f"Facturama no respondió ({_mensaje(exc)}); no se sabe si se canceló. Vuelve a intentar."})
        estatus = str((respuesta or {}).get("Status") or "").lower()
        with transaction.atomic():
            bloqueado = Cobro.objects.select_for_update(of=("self",)).get(pk=cobro.pk)
            bloqueado.rep_cancelacion_motivo = motivo
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
            bloqueado.save(update_fields=[*CAMPOS_REP, *CAMPOS_REP_CANCELACION, "updated_at"])
        _copiar(bloqueado, cobro, [*CAMPOS_REP, *CAMPOS_REP_CANCELACION])
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
                raise ErrorDeNegocio({"cfdi": PENDIENTE_CFDI})
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
        if not factura.facturama_id or not factura.cfdi_uuid or factura.cfdi_pendiente:
            raise ErrorDeNegocio({"cfdi": "La factura no tiene CFDI timbrado y confirmado."})
        if not factura.activo or factura.estatus == Factura.FacturaStatus.CANCELADA or factura.cfdi_cancelado:
            raise ErrorDeNegocio({"cfdi": "La factura o su CFDI está cancelado; no se envía."})
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
                archivo = servicio.get_cfdi_file(formato, "issued", factura.facturama_id)
                contenido = archivo.get("Content") if isinstance(archivo, dict) else None
                if not contenido:
                    raise ErrorDeNegocio({"cfdi": f"Facturama no entregó el {formato.upper()} del CFDI."})
                correo.attach(f"{nombre}.{formato}", base64.b64decode(contenido), tipo)
        except FacturamaAPIException as exc:
            raise ErrorDeNegocio({"cfdi": f"Facturama no entregó el archivo: {_mensaje(exc)}"})
        try:
            correo.send()
        except (SMTPException, OSError) as exc:
            raise ErrorDeNegocio({"email": f"No se pudo enviar el correo: {exc}"})
        return email

    @staticmethod
    def timbrar_nota_credito(nota):
        """Timbra el CFDI de egreso de una nota de crédito emitida. No toca saldos ni pólizas."""
        error = FacturaTimbradoService._timbrar_nota(nota)
        if error:
            raise error
        return nota

    @staticmethod
    def preparar_nota(n):
        """Valida y arma el CFDI de egreso; levanta ``ErrorDeNegocio`` si falta algo."""
        n = NotaCredito.objects.select_related(
            "factura__empresa", "factura__pedido__cliente_regimen_fiscal",
            "factura__cliente__sat_regimen_fiscal", "factura__sucursal",
        ).get(pk=n.pk)
        _exigir_empresa_duena(n.factura.empresa, "cfdi")
        if n.cfdi_uuid:
            raise ErrorDeNegocio({"cfdi": f"La nota ya está timbrada (UUID {n.cfdi_uuid})."})
        if n.cfdi_pendiente:
            raise ErrorDeNegocio({"cfdi": PENDIENTE_CFDI})
        if n.estatus != NotaCredito.Estatus.EMITIDA:
            raise ErrorDeNegocio({"estatus": "Solo se timbra una nota de crédito Emitida."})
        NotaCreditoService.validar_factura(n.factura)
        return construir_payload_nota_credito(n)

    @staticmethod
    def _timbrar_nota(nota):
        preparar = FacturaTimbradoService.preparar_nota
        obj, error = _timbrar(NotaCredito, nota.pk, preparar, CAMPOS_CFDI, "cfdi", "CFDI")
        _copiar(obj, nota, CAMPOS_CFDI)
        return error

    @staticmethod
    def conciliar_cfdi(obj):
        """Resuelve un ``cfdi_pendiente`` (factura o nota de crédito) contra Facturama (#758).

        Si el CFDI existe guarda UUID, ``facturama_id`` y fecha; si no, libera la reserva para
        reintentar ``timbrar/`` o cancelar. Regresa ``(resultado, aviso)``: ``"timbrado"`` o
        ``"liberado"``, y un aviso si el documento está cancelado o eliminado en el ERP pero su
        CFDI quedó vigente ante el SAT (#902). Facturama va fuera de toda transacción; si no
        responde no se toca nada.
        """
        modelo = type(obj)
        es_nota = modelo is NotaCredito
        actual = modelo.objects.select_related(
            *(("factura__empresa", "factura__cliente", "factura__pedido") if es_nota else ("empresa", "cliente", "pedido"))
        ).get(pk=obj.pk)
        factura = actual.factura if es_nota else actual
        _exigir_empresa_duena(factura.empresa, "cfdi")
        if not actual.cfdi_pendiente:
            raise ErrorDeNegocio({"cfdi": "No hay un timbrado sin confirmar que conciliar."})
        # Un timbrado en curso todavía puede contestar: no se libera su reserva a media llamada.
        # Se mide desde la reserva, no desde ``updated_at``, que mueve cualquier PATCH (#901).
        desde = actual.cfdi_pendiente_desde
        if desde and desde > timezone.now() - timedelta(seconds=int(settings.FACTURAMA_TIMEOUT) + 30):
            raise ErrorDeNegocio({"cfdi": "El timbrado es muy reciente; espera un minuto y vuelve a conciliar."})
        encontrado = _buscar_cfdi(actual, "E" if es_nota else "I", factura)
        with transaction.atomic():
            bloqueado = modelo.objects.select_for_update(of=("self",)).get(pk=obj.pk)
            if not bloqueado.cfdi_pendiente:
                raise ErrorDeNegocio({"cfdi": "Otra operación ya resolvió el timbrado; recarga."})
            if encontrado:
                bloqueado.cfdi_uuid, bloqueado.facturama_id, bloqueado.cfdi_fecha_timbrado = encontrado
            bloqueado.cfdi_pendiente = False
            bloqueado.cfdi_pendiente_desde = None
            bloqueado.save(update_fields=[*CAMPOS_CFDI, "cfdi_pendiente_desde", "updated_at"])
        _copiar(bloqueado, obj, CAMPOS_CFDI)
        if not encontrado:
            return "liberado", None
        aviso = None
        if bloqueado.estatus == modelo.Estatus.CANCELADA if es_nota else (
            bloqueado.estatus == Factura.FacturaStatus.CANCELADA or not bloqueado.activo
        ):
            # Texto para el usuario final, sin nombres de endpoints (EC-760).
            if es_nota:
                aviso = ("La nota de crédito está cancelada, pero su CFDI sigue vigente ante el SAT. "
                         "Pide a contabilidad que lo cancele en Facturama.")
            elif not bloqueado.activo:
                aviso = ("La factura fue eliminada, pero su CFDI sigue vigente ante el SAT. "
                         "Pide a contabilidad que lo cancele en Facturama.")
            else:
                aviso = ("La factura está cancelada, pero su CFDI sigue vigente ante el SAT. "
                         "Vuelve a cancelar la factura para cancelarlo también ante el SAT.")
        return "timbrado", aviso

    @staticmethod
    def cancelar_cfdi(factura, motivo_sat=None):
        """Cancela el CFDI ante el SAT con el motivo elegido (02/03, EC-806) y guarda el resultado.
        Levanta si no quedó cancelado."""
        error = FacturaTimbradoService._cancelar_cfdi(factura, motivo_sat)
        if error:
            raise error

    @staticmethod
    def _cancelar_cfdi(factura, motivo_sat):
        """Como ``cancelar_rep`` (#627): Facturama va fuera de toda transacción y sin la fila
        bloqueada; solo guardar el resultado la bloquea."""
        actual = Factura.objects.select_related("empresa").get(pk=factura.pk)
        if actual.cfdi_cancelado:
            return None
        _exigir_empresa_duena(actual.empresa, "cfdi")  # #564
        motivo = _motivo_sat(motivo_sat, actual.cfdi_cancelacion_motivo, actual.cfdi_cancelacion_solicitada)
        try:
            respuesta = FacturamaService().cancel_cfdi(actual.facturama_id, motivo)
        except FacturamaAPIException as exc:
            if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 408:
                return ErrorDeNegocio({"cfdi": f"Facturama no canceló el CFDI: {_mensaje(exc)}"})
            # #563: sin respuesta no se sabe si se canceló; reintentar es seguro.
            return ErrorDeNegocio({"cfdi": f"Facturama no respondió ({_mensaje(exc)}); no se sabe si se canceló. Vuelve a intentar."})
        estatus = str((respuesta or {}).get("Status") or "").lower()
        with transaction.atomic():
            bloqueada = Factura.objects.select_for_update(of=("self",)).get(pk=factura.pk)
            if not bloqueada.cfdi_cancelado:  # otra petición pudo guardarlo mientras tanto
                bloqueada.cfdi_cancelacion_motivo = motivo
                if estatus == "canceled":
                    bloqueada.cfdi_cancelado, bloqueada.cfdi_cancelacion_solicitada = True, False
                elif estatus in CANCELACION_PENDIENTE:  # #565, #630
                    bloqueada.cfdi_cancelacion_solicitada = True
                else:
                    # Rechazo del receptor (``active``): ya no hay solicitud en curso (#633).
                    bloqueada.cfdi_cancelacion_solicitada = False
                bloqueada.save(update_fields=[*CAMPOS_CFDI_CANCELACION, "updated_at"])
        _copiar(bloqueada, factura, CAMPOS_CFDI_CANCELACION)
        if bloqueada.cfdi_cancelado:
            return None
        if estatus in CANCELACION_PENDIENTE:
            return ErrorDeNegocio({"cancelacion_pendiente": MSG_CANCELACION_PENDIENTE})
        return ErrorDeNegocio({"cfdi": f"El SAT no canceló el CFDI (estatus: {estatus or 'vacío'})."})
