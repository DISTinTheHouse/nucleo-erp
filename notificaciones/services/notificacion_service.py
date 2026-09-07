from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from seguridad.role_identity import (
    canonicalizar_clave_departamento,
    inferir_clave_departamento,
    usuarios_ids_por_clave_departamento,
)

from ..models import Notificacion

logger = logging.getLogger(__name__)


def notificaciones_para_usuario(qs, user):
    """Sólo las notificaciones de las que ``user`` es el destinatario.

    ``Notificacion.usuario`` es NOT NULL y hay una fila por destinatario: esa FK
    ES el aislamiento, y no admite rama de admin/superuser —ser
    ``is_admin_empresa`` no te vuelve destinatario de la notificación de otro—
    ni necesita un filtro por empresa encima, que sólo podía restar filas
    propias. Es el mismo criterio que ``marcar_todas_leidas()`` y el stream SSE.
    """
    return qs.filter(usuario=user)


@transaction.atomic
def crear_notificacion_por_rol(
    empresa,
    codigo_rol: str | None,
    titulo: str,
    mensaje: str,
    modulo: str,
    tipo: str,
    data: dict | None = None,
    clave_departamento: str | None = None,
) -> int:
    clave_objetivo = canonicalizar_clave_departamento(
        clave_departamento or inferir_clave_departamento(codigo_rol)
    )
    usuarios_ids = usuarios_ids_por_clave_departamento(empresa, clave_objetivo)
    if not usuarios_ids:
        logger.warning(
            "No se encontraron usuarios activos para notificacion por rol",
            extra={
                "empresa_id": getattr(empresa, "pk", None),
                "clave_departamento": clave_objetivo,
                "codigo_rol": codigo_rol,
                "modulo": modulo,
                "tipo": tipo,
            },
        )
        return 0

    notifs = [
        Notificacion(
            empresa=empresa,
            usuario_id=uid,
            titulo=titulo,
            mensaje=mensaje,
            modulo=modulo,
            tipo=tipo,
            data=data,
        )
        for uid in usuarios_ids
    ]
    Notificacion.objects.bulk_create(notifs)
    return len(notifs)


@transaction.atomic
def marcar_leida(notificacion: Notificacion, user) -> Notificacion:
    from rest_framework.exceptions import PermissionDenied

    # Sin excepción para admin/superuser: marcar como leída la notificación de
    # otro se la desaparecía del pendiente a su verdadero destinatario.
    if notificacion.usuario_id != user.pk:
        raise PermissionDenied("No tienes acceso a esta notificación.")
    if not notificacion.leido:
        notificacion.leido = True
        notificacion.leido_at = timezone.now()
        notificacion.save(update_fields=["leido", "leido_at"])
    return notificacion


@transaction.atomic
def marcar_todas_leidas(user) -> int:
    qs = Notificacion.objects.filter(usuario=user, leido=False)
    count = qs.update(leido=True, leido_at=timezone.now())
    return count
