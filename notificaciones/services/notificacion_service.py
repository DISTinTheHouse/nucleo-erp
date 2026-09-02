from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import Notificacion


def _puede_ver_todo(user) -> bool:
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "is_admin_empresa", False)
    )


def _aplicar_scope_empresa(qs, user, lookup="empresa"):
    if _puede_ver_todo(user):
        return qs
    empresa = getattr(user, "empresa", None)
    if empresa is None:
        return qs.none()
    return qs.filter(**{lookup: empresa})


def notificaciones_para_usuario(qs, user):
    if _puede_ver_todo(user):
        return qs
    return qs.filter(usuario=user)


@transaction.atomic
def crear_notificacion_por_rol(
    empresa,
    codigo_rol: str,
    titulo: str,
    mensaje: str,
    modulo: str,
    tipo: str,
    data: dict | None = None,
) -> int:
    from seguridad.models import UsuarioRol
    from usuarios.models import Usuario

    usuarios_ids = (
        UsuarioRol.objects.filter(
            rol__empresa=empresa,
            rol__codigo__iexact=codigo_rol,
            rol__estatus="activo",
            usuario__estatus=Usuario.Estatus.ACTIVO,
        )
        .values_list("usuario_id", flat=True)
        .distinct()
    )
    if not usuarios_ids:
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

    if not _puede_ver_todo(user) and notificacion.usuario_id != user.pk:
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
