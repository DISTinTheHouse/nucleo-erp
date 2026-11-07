"""Timbrar el CFDI de una factura de venta con Facturama (``POST /facturas/{id}/timbrar/``).

Acción aparte de ``emitir``: no toca CxC, póliza ni estatus de la factura.
"""

import base64
from collections.abc import Callable
from dataclasses import dataclass
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


def _es_rechazo(exc):
    """Facturama contestó un 4xx (salvo 408): rechazo definitivo. Timeout, red, 408 o 5xx: no
    se sabe qué pasó del otro lado."""
    return bool(exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 408)


def agregar_observacion(observaciones, nota):
    """``observaciones`` con ``nota`` en una línea nueva; sin nota, igual."""
    return f"{observaciones or ''}\n{nota}".strip() if nota else observaciones


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
        if _es_rechazo(exc):
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


def _motivo_sat(recibido, guardado, fijado):
    """Motivo con que se pide la cancelación ante el SAT; lo elige el usuario, sin default (EC-806).

    ``fijado``: hay una solicitud que el SAT pudo registrar (solicitada y falta que el receptor
    acepte, o un intento sin respuesta). El reintento reenvía ese motivo: si no viene se reusa,
    y uno distinto es 400. Sin motivo guardado es ``02``: lo único que mandaba el código previo
    a EC-806 (una solicitud marcada entre la migración y el deploy).
    """
    recibido = normalizar_motivo_sat(recibido)
    if fijado:
        guardado = guardado or MotivoCancelacionSat.ERRORES_SIN_RELACION.value
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


def _limite_en_curso():
    """Una llamada a Facturama iniciada antes de esto ya terminó: su timeout más margen (#901)."""
    return timezone.now() - timedelta(seconds=int(settings.FACTURAMA_TIMEOUT) + 30)


@dataclass(frozen=True)
class _CancelacionSat:
    """Lo que cambia entre cancelar el CFDI de una factura y el de un REP (``_cancelar_ante_sat``).

    ``preparar(obj)`` valida con la fila bloqueada y regresa el id de Facturama, o ``None`` si
    no hay nada que cancelar. ``al_cancelar(obj)`` aplica un ``canceled`` del SAT.
    ``ya_cancelado(obj, facturama_id)``: otra petición ya guardó la cancelación de ese CFDI.
    ``campos``: lo que guarda el resultado. ``campo_nota``: dónde va la nota interna
    (``None``: no admite).
    """
    modelo: type
    prefijo: str
    nombre: str
    clave: str
    clave_incierto: str
    campos: tuple
    preparar: Callable
    al_cancelar: Callable
    ya_cancelado: Callable
    campo_nota: str | None = None

    def campo(self, sufijo):
        return f"{self.prefijo}_{sufijo}"


def _cancelar_ante_sat(cfg, pk, motivo_sat, nota=""):
    """Cancela un CFDI (factura o REP) ante el SAT con reserva previa, como ``_timbrar`` (EC-806).

    1) ``atomic`` corto con la fila bloqueada: ``cfg.preparar`` valida; si otra cancelación
       está en vuelo (``<prefijo>_en_curso_desde`` dentro de la ventana) es 400; se resuelve el
       motivo y se guardan motivo y reserva con ``update()`` (sin fila de historial: el
       resultado sí la deja). 2) Facturama, fuera de toda transacción. 3) ``atomic`` corto:
       guarda el resultado y, con ``canceled``, la ``nota`` en ``cfg.campo_nota``.

    Respuesta del SAT (``canceled``, ``pending``/``requested`` u otro estatus): definitiva, se
    libera la reserva. Rechazo 4xx de Facturama: este intento no ocurrió, así que vuelven el
    motivo y la reserva de antes; una reserva fresca se libera, una heredada de un intento sin
    respuesta se queda y sigue fijando el motivo. Timeout, red, 408 o 5xx: no se sabe si el SAT
    la registró, así que la reserva se queda y fija el motivo del reintento (#563, #623).

    Toda escritura posterior a Facturama revisa, con la fila bloqueada, que la reserva siga
    siendo la de esta petición (``<prefijo>_en_curso_desde`` igual al que guardó). Si otra
    petición reservó después (respuesta más lenta que la ventana), esta no toca reserva ni
    motivo: solo guarda lo que su respuesta confirma y no se puede perder (``canceled``: el CFDI
    ya no está vigente; ``pending``: el SAT tiene una solicitud) y descarta lo demás, que la
    petición vigente resolverá. Un 4xx sobre un CFDI que otra petición ya registró cancelado
    libera la reserva en vez de restaurar una heredada. Regresa ``(obj, error)``.
    """
    f_solicitada, f_motivo, f_desde = cfg.campo("solicitada"), cfg.campo("motivo"), cfg.campo("en_curso_desde")
    with transaction.atomic():
        obj = cfg.modelo.objects.select_for_update(of=("self",)).select_related("empresa").get(pk=pk)
        facturama_id = cfg.preparar(obj)
        if facturama_id is None:
            return obj, None
        desde_anterior, motivo_anterior = getattr(obj, f_desde), getattr(obj, f_motivo)
        if desde_anterior and desde_anterior > _limite_en_curso():
            raise ErrorDeNegocio({cfg.clave: MSG_CANCELACION_EN_CURSO.format(cfg.nombre)})
        fijado = getattr(obj, f_solicitada) or desde_anterior is not None
        motivo = _motivo_sat(motivo_sat, motivo_anterior, fijado)
        reservado = timezone.now()  # identifica la reserva de esta petición
        reserva = {f_motivo: motivo, f_desde: reservado, "updated_at": timezone.now()}
        cfg.modelo.objects.filter(pk=pk).update(**reserva)
        for campo, valor in reserva.items():
            setattr(obj, campo, valor)
    try:
        respuesta = FacturamaService().cancel_cfdi(facturama_id, motivo)
    except FacturamaAPIException as exc:
        if not _es_rechazo(exc):
            return obj, ErrorDeNegocio({cfg.clave_incierto: (
                f"Facturama no respondió ({_mensaje(exc)}); no se sabe si se canceló. "
                f"Vuelve a intentar en un minuto con el mismo motivo ({motivo})."
            )})
        # UPDATE sin fila de historial (no hay cambio neto), solo sobre la reserva propia.
        with transaction.atomic():
            fila = cfg.modelo.objects.select_for_update(of=("self",)).get(pk=pk)
            if getattr(fila, f_desde) == reservado:
                if cfg.ya_cancelado(fila, facturama_id):
                    previo = {f_desde: None}
                else:
                    previo = {f_motivo: motivo_anterior, f_desde: desde_anterior}
                cfg.modelo.objects.filter(pk=pk).update(**previo, updated_at=timezone.now())
                fila = cfg.modelo.objects.get(pk=pk)
        _copiar(fila, obj, (f_motivo, f_desde))
        return obj, ErrorDeNegocio({cfg.clave: f"Facturama no canceló el {cfg.nombre}: {_mensaje(exc)}"})
    estatus = str((respuesta or {}).get("Status") or "").lower()
    error = None
    campos = list(cfg.campos)
    with transaction.atomic():
        bloqueado = cfg.modelo.objects.select_for_update(of=("self",)).get(pk=pk)
        propia = getattr(bloqueado, f_desde) == reservado
        if propia:
            setattr(bloqueado, f_desde, None)
        guardar = propia
        if estatus == "canceled":
            cfg.al_cancelar(bloqueado)
            setattr(bloqueado, f_solicitada, False)
            guardar = True
            if nota and cfg.campo_nota:  # misma transacción que ``canceled`` (EC-806)
                setattr(bloqueado, cfg.campo_nota, agregar_observacion(getattr(bloqueado, cfg.campo_nota), nota))
                campos.append(cfg.campo_nota)
        elif estatus in CANCELACION_PENDIENTE:  # #565, #630
            setattr(bloqueado, f_solicitada, True)
            guardar = True
            error = ErrorDeNegocio({"cancelacion_pendiente": MSG_CANCELACION_PENDIENTE})
        else:
            # Rechazo del receptor (``active``): ya no hay solicitud en curso (#633). Si la
            # reserva ya es de otra petición, su respuesta decide.
            if propia:
                setattr(bloqueado, f_solicitada, False)
            error = ErrorDeNegocio({cfg.clave: f"El SAT no canceló el {cfg.nombre} (estatus: {estatus or 'vacío'})."})
        if guardar:
            bloqueado.save(update_fields=[*campos, "updated_at"])
    _copiar(bloqueado, obj, campos)
    return obj, error


CAMPOS_CFDI = ("cfdi_uuid", "facturama_id", "cfdi_fecha_timbrado", "cfdi_pendiente")
CAMPOS_REP = ("rep_uuid", "rep_facturama_id", "rep_fecha_timbrado", "rep_pendiente")
CAMPOS_REP_CANCELACION = (
    "rep_cancelado_uuid", "rep_fecha_cancelacion", "rep_cancelacion_solicitada", "rep_cancelacion_motivo",
    "rep_cancelacion_en_curso_desde",
)
CAMPOS_CFDI_CANCELACION = (
    "cfdi_cancelado", "cfdi_cancelacion_solicitada", "cfdi_cancelacion_motivo", "cfdi_cancelacion_en_curso_desde",
)
MSG_CANCELACION_EN_CURSO = (
    "Hay una cancelación del {} ante el SAT en curso; espera un minuto y vuelve a intentar."
)


def _preparar_cancelacion_cfdi(f):
    """Nada que cancelar solo si el CFDI ya está cancelado o la factura nunca se timbró. Con
    CFDI pero sin id de Facturama no se puede: marcarla hecha la dejaría vigente ante el SAT."""
    if f.cfdi_cancelado or not (f.cfdi_uuid or f.facturama_id):
        return None
    _exigir_empresa_duena(f.empresa, "cfdi")  # #564
    if not f.facturama_id:
        raise ErrorDeNegocio({"cfdi": (
            f"La factura tiene CFDI (UUID {f.cfdi_uuid}) pero no su id de Facturama: no se puede "
            "cancelar desde aquí. Revísalo en Facturama."
        )})
    return f.facturama_id


def _al_cancelar_cfdi(f):
    f.cfdi_cancelado = True


def _preparar_cancelacion_rep(c):
    _exigir_empresa_duena(c.empresa, "rep")
    if not c.rep_facturama_id:
        raise ErrorDeNegocio({"rep": "El cobro no tiene un REP en Facturama que cancelar."})
    return c.rep_facturama_id


def _al_cancelar_rep(c):
    c.rep_cancelado_uuid = c.rep_uuid or c.rep_cancelado_uuid
    c.rep_fecha_cancelacion = timezone.now()
    c.rep_uuid = c.rep_facturama_id = c.rep_fecha_timbrado = None
    c.rep_pendiente = False


CANCELACION_CFDI = _CancelacionSat(
    modelo=Factura, prefijo="cfdi_cancelacion", nombre="CFDI", clave="cfdi", clave_incierto="cfdi",
    campos=CAMPOS_CFDI_CANCELACION, preparar=_preparar_cancelacion_cfdi, al_cancelar=_al_cancelar_cfdi,
    ya_cancelado=lambda f, facturama_id: f.cfdi_cancelado,
    campo_nota="observaciones",
)
CANCELACION_REP = _CancelacionSat(
    modelo=Cobro, prefijo="rep_cancelacion", nombre="REP", clave="rep", clave_incierto="rep_incierto",
    campos=(*CAMPOS_REP, *CAMPOS_REP_CANCELACION), preparar=_preparar_cancelacion_rep,
    al_cancelar=_al_cancelar_rep,
    # Al cancelarse el REP se limpia ``rep_facturama_id``: ya no es el que se mandó a cancelar.
    ya_cancelado=lambda c, facturama_id: c.rep_facturama_id != facturama_id,
)
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
            if not _es_rechazo(exc):
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
        """Cancela el REP ante el SAT con el motivo elegido (EC-806), con reserva previa y
        Facturama fuera de toda transacción (#627; ver ``_cancelar_ante_sat``).

        ``canceled``: el UUID pasa a ``rep_cancelado_uuid`` (#625). ``pending``/``requested``:
        queda ``rep_cancelacion_solicitada`` (#624) y se reintenta cuando el receptor acepte.
        Se cancela con ``rep_facturama_id`` aunque falte el UUID (timbrado sin confirmar, #622).
        """
        obj, error = _cancelar_ante_sat(CANCELACION_REP, cobro.pk, motivo_sat)
        _copiar(obj, cobro, CANCELACION_REP.campos)
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
        if desde and desde > _limite_en_curso():
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
    def cancelar_cfdi(factura, motivo_sat=None, nota=""):
        """Cancela el CFDI ante el SAT con el motivo elegido (02/03, EC-806) y guarda el resultado.
        Levanta si no quedó cancelado. ``nota`` va a ``observaciones`` en la misma transacción
        que ``cfdi_cancelado``."""
        error = FacturaTimbradoService._cancelar_cfdi(factura, motivo_sat, nota)
        if error:
            raise error

    @staticmethod
    def _cancelar_cfdi(factura, motivo_sat, nota=""):
        """Como ``cancelar_rep`` (``_cancelar_ante_sat``): reserva con la fila bloqueada y
        Facturama fuera de toda transacción. Un CFDI ya cancelado no llama a nadie."""
        obj, error = _cancelar_ante_sat(CANCELACION_CFDI, factura.pk, motivo_sat, nota)
        _copiar(obj, factura, (*CANCELACION_CFDI.campos, "observaciones"))
        return error
