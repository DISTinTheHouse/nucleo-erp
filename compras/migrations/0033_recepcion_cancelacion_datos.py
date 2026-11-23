"""Recupera motivo, fecha y usuario de las recepciones ya canceladas (EC-926).

``POST recepciones/{id}/cancelar/`` (#1307) solo los dejaba en el evento
``CANCELAR`` de auditoría (``despues_json["motivo"]``, ``created_at``,
``usuario``). Las canceladas sin ese evento quedan en nulo. No pisa valores ya
escritos; el reverso no borra nada (los campos se eliminan con 0032).
"""

from django.db import migrations


def recuperar(apps, schema_editor):
    Recepcion = apps.get_model("compras", "Recepcion")
    AuditoriaEvento = apps.get_model("auditoria", "AuditoriaEvento")
    canceladas = Recepcion.objects.filter(estatus=6, motivo_cancelacion__isnull=True)
    for recepcion in canceladas.iterator():
        evento = (
            AuditoriaEvento.objects.filter(
                modulo="compras", accion="CANCELAR", tabla="recepciones", id_registro=str(recepcion.pk),
            )
            .order_by("-created_at", "-id_evento")
            .first()
        )
        motivo = ((evento.despues_json or {}).get("motivo") if evento else None) or None
        if not motivo:
            continue
        Recepcion.objects.filter(pk=recepcion.pk).update(
            motivo_cancelacion=str(motivo)[:500],
            fecha_cancelacion=evento.created_at,
            cancelado_por_id=evento.usuario_id,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("compras", "0032_recepcion_cancelacion"),
        ("auditoria", "0007_bitacora_etiquetas_uniformes"),
    ]

    operations = [migrations.RunPython(recuperar, migrations.RunPython.noop)]
