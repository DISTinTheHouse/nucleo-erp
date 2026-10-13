"""Cambio de ``OrdenProduccion.estatus_op`` en un clic.

Concentra el mapa de transiciones, los requisitos que salen de la ruta crítica
y el registro en auditoría, para que la vista sea solo transporte.
"""
from django.db import transaction
from django.utils import timezone

from auditoria.models import AuditoriaEvento
from produccion.models import OrdenProduccion, OrdenProduccionRutaCritica

E = OrdenProduccion.EstatusOrdenProduccion

# Estatus de los que ya no se sale (el negocio ya cerró la OP).
TERMINALES = frozenset({E.COMPLETADO, E.CANCELADO})

# Siguientes estatus permitidos desde cada uno. El flujo normal es
# Pendiente -> Preparación -> En producción -> Revisión -> Completado; Detenido
# y Cancelado se pueden alcanzar desde cualquier estatus abierto, y desde
# Detenido se reanuda en la etapa que corresponda.
TRANSICIONES = {
    E.PENDIENTE: (E.PREPARACION, E.DETENIDO, E.CANCELADO),
    E.PREPARACION: (E.BORDANDO, E.DETENIDO, E.CANCELADO),
    E.BORDANDO: (E.REVISION, E.PREPARACION, E.DETENIDO, E.CANCELADO),
    E.REVISION: (E.COMPLETADO, E.BORDANDO, E.DETENIDO, E.CANCELADO),
    E.DETENIDO: (E.PREPARACION, E.BORDANDO, E.REVISION, E.CANCELADO),
    E.COMPLETADO: (),
    E.CANCELADO: (),
}

# Estatus que obligan a explicar por qué (queda en auditoría).
REQUIEREN_MOTIVO = frozenset({E.DETENIDO, E.CANCELADO})


def bloqueos_de_ruta_critica(destino, ruta):
    """Requisitos de la ruta crítica que faltan para entrar a ``destino``.

    Hoy solo "En producción" exige algo: desarrollo de producto liberado (con
    fecha de liberación del paquete técnico y sin estar en espera de muestra)
    y telas & avíos con el kit completo. ``ruta`` puede ser ``None`` (OP sin
    ruta capturada: no cumple nada).
    """
    if destino != E.BORDANDO:
        return []
    faltan = []
    liberado = (
        ruta is not None
        and ruta.fecha_liberacion_paquete_tecnico is not None
        and ruta.estatus_paquete_tecnico != OrdenProduccionRutaCritica.EstatusPaqueteTecnico.ESPERA_MUESTRA
    )
    if not liberado:
        faltan.append("Desarrollo de producto: falta liberar el paquete técnico.")
    if ruta is None or not ruta.kit_completo:
        faltan.append("Telas y avíos: falta marcar el kit completo.")
    return faltan


def puede_forzar(user):
    return bool(getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False))


def opciones(op, ruta, user):
    """Lista de destinos posibles para ``op`` (``ruta`` ya cargada o ``None``)."""
    puede = puede_forzar(user)
    resultado = []
    for destino in TRANSICIONES[E(op.estatus_op)]:
        bloqueos = bloqueos_de_ruta_critica(destino, ruta)
        resultado.append({
            "estatus_op": int(destino),
            "etiqueta": destino.label,
            "requiere_motivo": destino in REQUIEREN_MOTIVO,
            "permitido": not bloqueos,
            "bloqueos": bloqueos,
            "se_puede_forzar": bool(bloqueos) and puede,
        })
    return resultado


class EstatusOpError(Exception):
    """Cambio rechazado. ``codigo`` es el HTTP status; ``detalle`` el cuerpo."""

    def __init__(self, codigo, detalle):
        super().__init__(str(detalle))
        self.codigo = codigo
        self.detalle = detalle


def cambiar_estatus(op_id, visibles, user, destino, motivo="", forzar=False, request=None):
    """Aplica el cambio de forma atómica y deja el evento en auditoría.

    ``visibles`` es el queryset de OPs visibles para el usuario (alcance
    multi-tenant ya resuelto por la vista). Devuelve la OP actualizada.
    """
    try:
        destino = E(destino)
    except ValueError:
        raise EstatusOpError(400, {"estatus_op": "Estatus no válido."})

    motivo = (motivo or "").strip()

    with transaction.atomic():
        # Bloqueo de fila: dos clics simultáneos no pueden aplicar dos
        # transiciones sobre el mismo estatus de partida.
        op = visibles.select_for_update().filter(pk=op_id).first()
        if op is None:
            raise EstatusOpError(404, {"msg": "Orden de producción no encontrada"})

        origen = E(op.estatus_op)
        if destino not in TRANSICIONES[origen]:
            raise EstatusOpError(409, {
                "msg": f"No se puede pasar de {origen.label} a {destino.label}.",
                "estatus_op": int(origen),
            })
        if destino in REQUIEREN_MOTIVO and not motivo:
            raise EstatusOpError(400, {"motivo": f"El motivo es obligatorio para pasar a {destino.label}."})

        ruta = OrdenProduccionRutaCritica.objects.filter(op_id=op.pk).first() if destino == E.BORDANDO else None
        bloqueos = bloqueos_de_ruta_critica(destino, ruta)
        forzado = False
        if bloqueos:
            if not (forzar and puede_forzar(user)):
                raise EstatusOpError(409, {
                    "msg": "Faltan requisitos de la ruta crítica.",
                    "bloqueos": bloqueos,
                    "se_puede_forzar": puede_forzar(user),
                })
            forzado = True

        ahora = timezone.now()
        op.estatus_op = int(destino)
        campos = ["estatus_op"]
        # El KPI de cumplimiento a tiempo mide por fecha_fin de las OPs
        # Completadas; sin esto una OP cerrada desde aquí quedaría "sin clasificar".
        if destino == E.COMPLETADO and op.fecha_fin is None:
            op.fecha_fin = ahora
            campos.append("fecha_fin")
        op.save(update_fields=campos)

        ip = user_agent = None
        if request is not None:
            ip = request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR")
            user_agent = request.META.get("HTTP_USER_AGENT")
        AuditoriaEvento.objects.create(
            empresa_id=op.empresa_id,
            usuario=user if getattr(user, "pk", None) else None,
            modulo="produccion",
            accion="CAMBIO_ESTATUS",
            tabla="ordenes_produccion",
            id_registro=str(op.op_id),
            antes_json={"estatus_op": int(origen), "estatus": origen.label, "folio_op": op.folio_op},
            despues_json={
                "estatus_op": int(destino),
                "estatus": destino.label,
                "folio_op": op.folio_op,
                "motivo": motivo or None,
                "forzado": forzado,
                "bloqueos_omitidos": bloqueos if forzado else [],
            },
            ip=ip,
            user_agent=user_agent,
        )
    return op
