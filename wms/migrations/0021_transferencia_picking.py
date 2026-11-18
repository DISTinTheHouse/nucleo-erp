"""``Transferencia.picking`` (EC-916): el picking que originó cada TR.

``Picking.transferencia`` solo guarda la última TR (``trasladar()`` la pisa) y la
vista 360 del pedido encontraba las demás con un regex sobre ``observaciones``,
texto que cualquiera puede escribir por la API. Esta FK la fija el servidor.

Backfill: solo se liga una TR cuando no hay duda. Hace falta, a la vez:

1. El texto exacto que genera el código: ``Traspaso del picking <folio>`` (alta,
   ``PickingService.store``) o ``Traslado del picking <folio>`` (``trasladar``).
2. Un picking con ese folio en la MISMA empresa y sucursal que la TR (el folio
   no es único: sale de una serie por empresa/sucursal).
3. Evidencia de que es suya, porque el texto se puede falsificar:
   - una referencia estructural: ``Picking.transferencia`` o la auditoría
     ``TRASLADO_PICKING`` del picking (``antes_json``/``despues_json``
     ``transferencia_id``);
   - o, solo para el alta, que el picking se creó justo después de la TR (misma
     transacción en ``store``: la TR nace primero), dentro de ``VENTANA_ALTA``.

Queda en null (ambigua) si: ninguno o más de un candidato tiene evidencia; una
referencia estructural de la TR apunta a otro picking; o dos TR de alta reclaman
el mismo picking. Las TR manuales (texto libre) no se tocan.
"""

import re
from collections import defaultdict
from datetime import timedelta

import django.db.models.deletion
from django.db import migrations, models

PATRON = re.compile(r"^(Traspaso|Traslado) del picking (\S+)$")
ALTA, TRASLADO = "Traspaso", "Traslado"
VENTANA_ALTA = timedelta(seconds=60)


def _entero(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def planear_backfill(Transferencia, Picking, AuditoriaEvento):
    """``(asignaciones, ambiguas)``: ``{tr_id: picking_id}`` y los ids de las TR con
    el texto de un picking que se quedan sin ligar. Solo lee."""
    trs = list(
        Transferencia.objects.filter(observaciones__regex=r"^(Traspaso|Traslado) del picking \S+$")
        .values("id", "empresa_id", "sucursal_id", "observaciones", "fecha_creacion")
    )
    if not trs:
        return {}, []
    tr_ids = {t["id"] for t in trs}
    textos = {}
    for t in trs:
        m = PATRON.match(t["observaciones"] or "")
        if m:
            textos[t["id"]] = (m.group(1), m.group(2))

    folios = {folio for _, folio in textos.values()}
    candidatos = defaultdict(list)  # (folio, empresa, sucursal) -> [picking]
    for p in Picking.objects.filter(folio__in=folios).values(
        "id", "folio", "empresa_id", "sucursal_id", "created_at"
    ):
        candidatos[(p["folio"], p["empresa_id"], p["sucursal_id"])].append(p)

    # Referencias estructurales TR -> pickings.
    refs = defaultdict(set)
    for p in Picking.objects.filter(transferencia_id__in=tr_ids).values("id", "transferencia_id"):
        refs[p["transferencia_id"]].add(p["id"])
    for ev in AuditoriaEvento.objects.filter(accion="TRASLADO_PICKING", tabla="picking").values(
        "id_registro", "antes_json", "despues_json"
    ):
        picking_id = _entero(ev["id_registro"])
        if picking_id is None:
            continue
        for js in (ev["antes_json"], ev["despues_json"]):
            tr_id = _entero((js or {}).get("transferencia_id")) if isinstance(js, dict) else None
            if tr_id in tr_ids:
                refs[tr_id].add(picking_id)

    asignaciones, ambiguas, altas = {}, [], defaultdict(list)
    for t in trs:
        if t["id"] not in textos:
            ambiguas.append(t["id"])
            continue
        tipo, folio = textos[t["id"]]
        posibles = candidatos.get((folio, t["empresa_id"], t["sucursal_id"]), [])
        posibles_ids = {p["id"] for p in posibles}
        referidos = refs.get(t["id"], set())

        def con_evidencia(p):
            if p["id"] in referidos:
                return True
            if tipo == ALTA and t["fecha_creacion"] and p["created_at"]:
                return timedelta(0) <= p["created_at"] - t["fecha_creacion"] <= VENTANA_ALTA
            return False

        elegidos = [p["id"] for p in posibles if con_evidencia(p)]
        if len(elegidos) != 1 or (referidos - posibles_ids):
            ambiguas.append(t["id"])
            continue
        asignaciones[t["id"]] = elegidos[0]
        if tipo == ALTA:
            altas[elegidos[0]].append(t["id"])

    # Un picking tiene una sola TR de alta: dos que lo reclaman son ambiguas.
    for repetidas in (ids for ids in altas.values() if len(ids) > 1):
        for tr_id in repetidas:
            asignaciones.pop(tr_id, None)
            ambiguas.append(tr_id)
    return asignaciones, sorted(ambiguas)


def ligar_transferencias(apps, schema_editor):
    Transferencia = apps.get_model("wms", "Transferencia")
    asignaciones, _ = planear_backfill(
        Transferencia, apps.get_model("wms", "Picking"), apps.get_model("auditoria", "AuditoriaEvento"),
    )
    por_picking = defaultdict(list)
    for tr_id, picking_id in asignaciones.items():
        por_picking[picking_id].append(tr_id)
    for picking_id, ids in por_picking.items():
        Transferencia.objects.filter(pk__in=ids, picking__isnull=True).update(picking_id=picking_id)


class Migration(migrations.Migration):

    dependencies = [
        ('auditoria', '0007_bitacora_etiquetas_uniformes'),
        ('wms', '0020_rfid_lectura_ligada_a_etiqueta'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicaltransferencia',
            name='picking',
            field=models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='wms.picking'),
        ),
        migrations.AddField(
            model_name='transferencia',
            name='picking',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transferencias', to='wms.picking'),
        ),
        # Al revertir, el RemoveField de la columna deshace el backfill.
        migrations.RunPython(ligar_transferencias, migrations.RunPython.noop),
    ]
