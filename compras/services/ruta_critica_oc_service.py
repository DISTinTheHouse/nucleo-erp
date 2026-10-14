"""Ruta crítica de la orden de compra (informativa).

Compras captura la fecha real de recibo y un comentario. Nada de esto toca el
estatus de la OC, las recepciones ni el inventario: leer y escribir solo pasa
por ``ordenes_compra_ruta_critica`` y por la auditoría.
"""
from django.db import transaction
from django.db.models import Min
from django.utils import timezone

from auditoria.models import AuditoriaEvento
from compras.models import OrdenCompra, OrdenCompraRutaCritica, Recepcion
from seguridad.role_identity import (
    CLAVE_DEPARTAMENTO_COMPRAS,
    CLAVE_DEPARTAMENTO_CONTA_COMPRAS,
    claves_departamento_usuario,
)

E = OrdenCompra.EstatusOrdenCompra

# Solo una OC ya autorizada puede llegar: antes es borrador y CANCELADA es terminal.
ESTATUS_CAPTURABLES = frozenset({E.AUTORIZADA, E.PARCIALMENTE_RECIBIDA, E.RECIBIDA})

CAMPOS = ("fecha_real_recibo", "comentarios")
MAX_COMENTARIOS = 2000
MAX_OCS_BULK = 200


class RutaCriticaOCError(Exception):
    """Captura rechazada. ``codigo`` es el HTTP status; ``detalle`` el cuerpo."""

    def __init__(self, codigo, detalle):
        super().__init__(str(detalle))
        self.codigo = codigo
        self.detalle = detalle


def puede_capturar(user):
    if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
        return True
    claves = claves_departamento_usuario(user, empresa=getattr(user, "empresa", None))
    return bool(claves & {CLAVE_DEPARTAMENTO_COMPRAS, CLAVE_DEPARTAMENTO_CONTA_COMPRAS})


def ocs_de_la_empresa(user):
    empresa = getattr(user, "empresa", None)
    qs = OrdenCompra.objects.filter(activo=True).only(
        "id", "folio", "estatus", "fecha_entrega_estimada", "empresa"
    )
    return qs.filter(empresa=empresa) if empresa is not None else qs.none()


def _primera_recepcion_wms(oc_ids):
    """``{oc_id: primera fecha de recepción}`` en una sola consulta."""
    filas = (
        Recepcion.objects.filter(
            orden_compra_id__in=oc_ids,
            tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA,
            activo=True,
        )
        .exclude(estatus__in=[Recepcion.EstatusRecepcion.CANCELADA, Recepcion.EstatusRecepcion.BORRADOR])
        .values("orden_compra_id")
        .annotate(primera=Min("fecha_recepcion"))
    )
    return {f["orden_compra_id"]: f["primera"] for f in filas}


def _nombre(usuario):
    if usuario is None:
        return None
    return usuario.get_full_name() or usuario.get_username()


def _fila(oc, ruta, primera_recepcion):
    return {
        "oc_id": oc.pk,
        "folio": oc.folio,
        "estatus": oc.estatus,
        "estatus_display": oc.get_estatus_display(),
        "fecha_entrega_estimada": oc.fecha_entrega_estimada,
        "fecha_recepcion_wms": primera_recepcion,
        "fecha_real_recibo": ruta.fecha_real_recibo if ruta else None,
        "comentarios": ruta.comentarios if ruta else None,
        "actualizado_por": ruta.updated_by_id if ruta else None,
        "actualizado_por_nombre": _nombre(ruta.updated_by) if ruta and ruta.updated_by_id else None,
        "updated_at": ruta.updated_at if ruta else None,
    }


def leer(ocs):
    """Filas de ruta crítica para ``ocs`` (lista de OrdenCompra): 2 consultas,
    sin importar cuántas. Las OC sin captura salen con los valores por defecto."""
    ids = [oc.pk for oc in ocs]
    rutas = {
        r.oc_id: r
        for r in OrdenCompraRutaCritica.objects.filter(oc_id__in=ids).select_related("updated_by")
    }
    wms = _primera_recepcion_wms(ids)
    return [_fila(oc, rutas.get(oc.pk), wms.get(oc.pk)) for oc in ocs]


def validar(datos):
    """Normaliza y valida el body del PATCH. Devuelve solo los campos enviados."""
    desconocidos = set(datos) - set(CAMPOS)
    if desconocidos:
        raise RutaCriticaOCError(400, {c: "Campo no editable." for c in sorted(desconocidos)})
    limpio = {}
    if "fecha_real_recibo" in datos:
        valor = datos["fecha_real_recibo"]
        if valor in (None, ""):
            limpio["fecha_real_recibo"] = None
        else:
            from django.utils.dateparse import parse_date
            try:
                fecha = parse_date(valor) if isinstance(valor, str) else None
            except ValueError:
                fecha = None
            if fecha is None:
                raise RutaCriticaOCError(400, {"fecha_real_recibo": "Fecha inválida (AAAA-MM-DD)."})
            if fecha > timezone.localdate():
                raise RutaCriticaOCError(400, {"fecha_real_recibo": "No puede ser una fecha futura."})
            limpio["fecha_real_recibo"] = fecha
    if "comentarios" in datos:
        valor = datos["comentarios"]
        if valor is not None and not isinstance(valor, str):
            raise RutaCriticaOCError(400, {"comentarios": "Debe ser texto."})
        valor = (valor or "").strip()
        if len(valor) > MAX_COMENTARIOS:
            raise RutaCriticaOCError(400, {"comentarios": f"Máximo {MAX_COMENTARIOS} caracteres."})
        limpio["comentarios"] = valor or None
    return limpio


def _json(valores):
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in valores.items()}


def capturar(oc_id, user, datos, ip=None, user_agent=None):
    """Guarda la captura y deja el evento en auditoría con quién y qué cambió.

    Solo escribe en ``ordenes_compra_ruta_critica`` (la OC no se guarda, así que
    no genera historial de OC ni toca sus recepciones). Devuelve la OC.
    """
    if not puede_capturar(user):
        raise RutaCriticaOCError(400, {"permiso": "Acción disponible solo para compras."})
    cambios = validar(datos)

    with transaction.atomic():
        oc = ocs_de_la_empresa(user).select_for_update().filter(pk=oc_id).first()
        if oc is None:
            raise RutaCriticaOCError(404, {"msg": "Orden de compra no encontrada"})
        if oc.estatus not in ESTATUS_CAPTURABLES:
            raise RutaCriticaOCError(409, {
                "msg": f"La OC está {oc.get_estatus_display().lower()}: su ruta crítica solo se captura desde Autorizada.",
            })
        ruta, _ = OrdenCompraRutaCritica.objects.get_or_create(oc_id=oc.pk)
        antes = {c: getattr(ruta, c) for c in cambios}
        reales = {c: v for c, v in cambios.items() if antes[c] != v}
        if reales:
            for campo, valor in reales.items():
                setattr(ruta, campo, valor)
            ruta.updated_by = user
            ruta.save(update_fields=[*reales, "updated_by", "updated_at"])
            AuditoriaEvento.objects.create(
                empresa_id=oc.empresa_id,
                usuario=user if getattr(user, "pk", None) else None,
                modulo="compras",
                accion="RUTA_CRITICA",
                tabla=OrdenCompraRutaCritica._meta.db_table,
                id_registro=str(oc.pk),
                antes_json={"folio": oc.folio, **_json({c: antes[c] for c in reales})},
                despues_json={"folio": oc.folio, **_json(reales)},
                ip=ip,
                user_agent=user_agent,
            )
    return oc
