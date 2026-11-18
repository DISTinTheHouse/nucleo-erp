"""Liga las TR históricas con su picking: ``Transferencia.picking`` (EC-916).

``PickingService`` fija la FK al crear cada TR desde un picking (alta y
traslados). Este servicio la rellena en las TR que quedaron en null: las de antes
de la migración ``wms.0021`` y las que crea el build anterior mientras se
despliega (o tras un rollback). Lo llaman la migración 0021 y el comando
``ligar_transferencias_picking``, que el pipeline corre tras cada despliegue.

Idempotente: solo escribe TR con ``picking`` null, con las mismas reglas cada vez.
Solo se liga una TR cuando no hay duda; hace falta, a la vez:

1. El texto exacto que genera el código: ``Traspaso del picking <folio>`` (alta,
   ``PickingService.handle_store``) o ``Traslado del picking <folio>``
   (``trasladar``).
2. Un picking con ese folio en la MISMA empresa y sucursal que la TR (el folio
   no es único: sale de una serie por empresa/sucursal).
3. Evidencia de que es suya, porque el texto se puede falsificar:
   - estructural: ``Picking.transferencia``, la auditoría ``TRASLADO_PICKING``
     del picking (``antes_json``/``despues_json`` ``transferencia_id``) o la
     propia ``Transferencia.picking`` ya fijada;
   - o, solo para el alta, que el picking se creó justo después de la TR (misma
     transacción en ``handle_store``: la TR nace primero), dentro de
     ``VENTANA_ALTA``.

Queda en null (ambigua) si: ninguno o más de un candidato tiene evidencia; una
referencia estructural apunta a otro picking; o varias TR de alta reclaman el
mismo picking y no hay exactamente una con evidencia estructural (si la hay, se
queda esa y se descartan las que solo tenían la ventana). Las TR manuales (texto
libre) no se tocan.

Recibe los modelos como parámetros para que la migración le pase los históricos.
"""

import re
from collections import defaultdict
from datetime import timedelta

PATRON = re.compile(r"^(Traspaso|Traslado) del picking (\S+)$")
PATRON_SQL = r"^(Traspaso|Traslado) del picking \S+$"
ALTA, TRASLADO = "Traspaso", "Traslado"
VENTANA_ALTA = timedelta(seconds=60)


def _entero(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def planear(Transferencia, Picking, AuditoriaEvento):
    """``(asignaciones, ambiguas)``: ``{tr_id: picking_id}`` de las TR en null que
    se pueden ligar y los ids de las que tienen el texto de un picking y se quedan
    en null. Solo lee."""
    trs = list(
        Transferencia.objects.filter(observaciones__regex=PATRON_SQL).values(
            "id", "empresa_id", "sucursal_id", "observaciones", "fecha_creacion", "picking_id"
        )
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
    for t in trs:
        if t["picking_id"]:
            refs[t["id"]].add(t["picking_id"])
    for p in Picking.objects.filter(transferencia_id__in=tr_ids).values("id", "transferencia_id"):
        refs[p["transferencia_id"]].add(p["id"])
    for ev in AuditoriaEvento.objects.filter(accion="TRASLADO_PICKING", tabla="picking").values(
        "id_registro", "antes_json", "despues_json"
    ):
        picking_id = _entero(ev["id_registro"])
        if picking_id is None:
            continue
        for js in (ev["antes_json"], ev["despues_json"]):
            tr_id = _entero(js.get("transferencia_id")) if isinstance(js, dict) else None
            if tr_id in tr_ids:
                refs[tr_id].add(picking_id)

    elegidas, ambiguas = {}, []
    altas = defaultdict(list)  # picking_id -> [(tr_id, estructural)]
    for t in trs:
        if t["id"] not in textos:
            ambiguas.append(t["id"])
            continue
        tipo, folio = textos[t["id"]]
        posibles = candidatos.get((folio, t["empresa_id"], t["sucursal_id"]), [])
        posibles_ids = {p["id"] for p in posibles}
        referidos = refs.get(t["id"], set())

        def en_ventana(p, t=t):
            return bool(
                t["fecha_creacion"] and p["created_at"]
                and timedelta(0) <= p["created_at"] - t["fecha_creacion"] <= VENTANA_ALTA
            )

        elegidos = [
            p["id"] for p in posibles
            if p["id"] in referidos or (tipo == ALTA and en_ventana(p))
        ]
        if len(elegidos) != 1 or (referidos - posibles_ids):
            ambiguas.append(t["id"])
            continue
        elegidas[t["id"]] = elegidos[0]
        if tipo == ALTA:
            altas[elegidos[0]].append((t["id"], elegidos[0] in referidos))

    # Un picking tiene una sola TR de alta. Si varias lo reclaman, se queda la única
    # con evidencia estructural; sin ella (o con varias), ninguna.
    for reclamantes in (r for r in altas.values() if len(r) > 1):
        estructurales = [tr_id for tr_id, estructural in reclamantes if estructural]
        conservar = estructurales[0] if len(estructurales) == 1 else None
        for tr_id, _ in reclamantes:
            if tr_id != conservar:
                elegidas.pop(tr_id, None)
                ambiguas.append(tr_id)

    pendientes = {t["id"] for t in trs if not t["picking_id"]}
    asignaciones = {tr: p for tr, p in elegidas.items() if tr in pendientes}
    return asignaciones, sorted(set(ambiguas) & pendientes)


def ligar(Transferencia, Picking, AuditoriaEvento):
    """Fija ``picking`` en las TR en null que ``planear`` asigna. Devuelve
    ``(ligadas, ambiguas)``. ``QuerySet.update()``: no es un cambio de estado que
    deba quedar en el historial ni en la bitácora."""
    asignaciones, ambiguas = planear(Transferencia, Picking, AuditoriaEvento)
    por_picking = defaultdict(list)
    for tr_id, picking_id in asignaciones.items():
        por_picking[picking_id].append(tr_id)
    ligadas = 0
    for picking_id, ids in por_picking.items():
        ligadas += Transferencia.objects.filter(pk__in=ids, picking__isnull=True).update(picking_id=picking_id)
    return ligadas, ambiguas
