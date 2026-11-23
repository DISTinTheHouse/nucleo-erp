"""Recupera motivo, fecha y usuario de las recepciones ya canceladas (EC-926).

``POST recepciones/{id}/cancelar/`` (#1307) solo los dejaba en el evento
``CANCELAR`` de auditoría (``despues_json["motivo"]``, ``created_at``,
``usuario``). Las canceladas sin ese evento quedan en nulo. No pisa valores ya
escritos; el reverso no borra nada (los campos se eliminan con 0032).

Antes de EC-926 el motivo no tenía tope: uno de más de 500 caracteres se corta
a 500 (el largo de ``motivo_cancelacion``). El motivo completo sigue en
``AuditoriaEvento.despues_json``; la migración informa cuántas filas cortó.
"""

from django.db import migrations

MOTIVO_MAX = 500


def recuperar(apps, schema_editor):
    Recepcion = apps.get_model("compras", "Recepcion")
    AuditoriaEvento = apps.get_model("auditoria", "AuditoriaEvento")
    canceladas = Recepcion.objects.filter(estatus=6, motivo_cancelacion__isnull=True)
    recuperadas = cortadas = 0
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
        motivo = str(motivo)
        cortadas += len(motivo) > MOTIVO_MAX
        recuperadas += 1
        Recepcion.objects.filter(pk=recepcion.pk).update(
            motivo_cancelacion=motivo[:MOTIVO_MAX],
            fecha_cancelacion=evento.created_at,
            cancelado_por_id=evento.usuario_id,
        )
    if recuperadas:
        print(
            f"\n  compras 0033: {recuperadas} recepción(es) canceladas recuperadas de auditoría; "
            f"{cortadas} con el motivo cortado a {MOTIVO_MAX} caracteres (completo en AuditoriaEvento)."
        )
    return recuperadas, cortadas


class Migration(migrations.Migration):

    dependencies = [
        ("compras", "0032_recepcion_cancelacion"),
        ("auditoria", "0007_bitacora_etiquetas_uniformes"),
    ]

    operations = [migrations.RunPython(recuperar, migrations.RunPython.noop)]
