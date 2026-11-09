"""Pre-orden de producción de Fabricación especial.

Flujo: el BOM de una variante de producción de muestra crea/actualiza la
pre-orden del pedido (``sincronizar_pre_orden``) → almacén confirma existencias
(``confirmar_existencias``; cada material que falta queda como
``compras.NecesidadCompra``) → compras consolida necesidades en OC → al recibir
se reparten (``repartir_recepcion_oc``) y ``recalcular`` mueve la pre-orden entre
Faltan materiales / En compra / Lista para producir → ``crear-op`` la convierte.

No aparta material: la existencia es la de hoy en los almacenes de la sucursal
de la pre-orden (el mismo alcance con el que ``OrdenProduccionService`` descuenta).
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import models, transaction
from django.db.models import Exists, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from catalogo.models import VarianteProductoProduccion
from inventarios.models import Existencia
from produccion.models import BomDetalle, PreOrdenEvento, PreOrdenProduccion, PreOrdenProduccionDetalle
from ventas.models import Pedido, PedidoDetalleTalla
from ventas.services.sku_produccion_service import detalles_de_muestra

E = PreOrdenProduccion.Estatus
EV = PreOrdenEvento.Evento
ABIERTAS = (E.PENDIENTE_VALIDAR, E.FALTAN_MATERIALES, E.EN_COMPRA, E.LISTA)
# Estatus que se recalculan solos (ya validados por almacén).
RECALCULABLES = (E.FALTAN_MATERIALES, E.EN_COMPRA, E.LISTA)
Q4 = Decimal("0.0001")
EPS = Decimal("0.0001")
CERO = Decimal("0")
DIAS_AMARILLO = 3


# --------------------------------------------------------------------------- utilidades

def evento(pre, tipo, usuario=None, descripcion=""):
    PreOrdenEvento.objects.create(
        pre_orden=pre, evento=tipo, usuario=usuario if getattr(usuario, "pk", None) else None,
        descripcion=(descripcion or "")[:500],
    )


def _cambiar_estatus(pre, nuevo, campos=None):
    """Pone ``nuevo`` y sella ``estatus_desde``; devuelve los campos a guardar."""
    campos = list(campos or [])
    if nuevo != pre.estatus:
        pre.estatus = nuevo
        pre.estatus_desde = timezone.now()
        campos += ["estatus", "estatus_desde"]
    return campos


def fecha_compromiso(pedido):
    from ventas.services.clasificacion_service import rango_fecha_entrega
    return rango_fecha_entrega(pedido)[1] if pedido else None


def semaforo(estatus, faltantes, fecha_estimada, compromiso):
    """``verde`` | ``amarillo`` | ``rojo`` (``None`` si está cancelada).

    Sin faltantes (o convertida) → verde. Con faltantes: sin fecha estimada del
    material → amarillo; la fecha estimada pasa del compromiso → rojo; llega a
    menos de 3 días del compromiso → amarillo; si no → verde.
    """
    if estatus == E.CANCELADA:
        return None
    if estatus == E.CONVERTIDA or (estatus != E.PENDIENTE_VALIDAR and not faltantes):
        return "verde"
    if not faltantes:
        return "verde"
    if fecha_estimada is None or compromiso is None:
        return "amarillo"
    if fecha_estimada > compromiso:
        return "rojo"
    if fecha_estimada > compromiso - timedelta(days=DIAS_AMARILLO):
        return "amarillo"
    return "verde"


def pre_orden_abierta(pedido_id, lock=False):
    qs = PreOrdenProduccion.objects.filter(pedido_id=pedido_id, estatus__in=ABIERTAS)
    if lock:
        qs = qs.select_for_update()
    return qs.first()


# --------------------------------------------------------------------------- requeridos

def requeridos(pre_ids, empresa_id):
    """``{pre_id: {producto_id: {requerido, nombre, codigo, unidad, unidad_id}}}``
    para varias pre-órdenes en 2 consultas (renglones + BOM vigente)."""
    piezas = defaultdict(dict)
    for pid, vid, cant in PreOrdenProduccionDetalle.objects.filter(pre_orden_id__in=list(pre_ids)).values_list(
        "pre_orden_id", "variante_produccion_id", "cantidad"
    ):
        piezas[pid][vid] = Decimal(str(cant))
    variantes = {v for d in piezas.values() for v in d}
    filas = list(
        BomDetalle.objects.filter(
            bom__variante_produccion_id__in=list(variantes), bom__activo=True, bom__empresa_id=empresa_id,
            activo=True, componente__isnull=False,
        ).values(
            "bom_id", "bom__variante_produccion_id", "componente_id", "componente__nombre",
            "componente__codigo", "componente__cod_proscai", "unidad_id", "unidad__clave", "cantidad", "desperdicio",
        )
    )
    # BOM vigente por variante: el más reciente activo (mismo criterio que el onboarding de OP).
    vigente = {}
    for f in filas:
        v = f["bom__variante_produccion_id"]
        vigente[v] = max(vigente.get(v, 0), f["bom_id"])
    por_variante = defaultdict(list)
    for f in filas:
        if f["bom_id"] == vigente[f["bom__variante_produccion_id"]]:
            por_variante[f["bom__variante_produccion_id"]].append(f)
    out = {}
    for pid, vs in piezas.items():
        req = {}
        for vid, n in vs.items():
            for f in por_variante.get(vid, []):
                factor = Decimal("1") + Decimal(str(f["desperdicio"] or 0)) / Decimal("100")
                cant = (n * Decimal(str(f["cantidad"] or 0)) * factor).quantize(Q4, ROUND_HALF_UP)
                m = req.setdefault(f["componente_id"], {
                    "producto_id": f["componente_id"],
                    "codigo": f["componente__cod_proscai"] or f["componente__codigo"] or "",
                    "nombre": f["componente__nombre"],
                    "unidad": (f["unidad__clave"] or "").upper(),
                    "unidad_id": f["unidad_id"],
                    "requerido": CERO,
                })
                m["requerido"] += cant
        out[pid] = req
    return out


def existencias(empresa_id, productos, sucursal_id=None):
    """``{producto_id: cantidad}`` (producto o sus variantes). 1 consulta."""
    if not productos:
        return {}
    qs = Existencia.objects.filter(almacen__empresa_id=empresa_id)
    if sucursal_id is not None:
        qs = qs.filter(almacen__sucursal_id=sucursal_id)
    return {
        pid: Decimal(str(t or 0))
        for pid, t in qs.filter(Q(producto_id__in=list(productos)) | Q(producto_variante__producto_id__in=list(productos)))
        .annotate(pid=Coalesce("producto_id", "producto_variante__producto_id"))
        .values("pid").annotate(t=Sum("cantidad")).values_list("pid", "t")
    }


def en_transito(empresa_id, productos, sucursal_id=None):
    """``{producto_id: cantidad}`` pedida en OC autorizadas/parciales activas y
    aún no recibida (neto de recepciones no canceladas). 1 consulta."""
    from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
    if not productos:
        return {}
    num = models.DecimalField(max_digits=18, decimal_places=4)
    recibido = Coalesce(
        Subquery(
            RecepcionDetalle.objects.filter(orden_compra_detalle_id=OuterRef("pk"), recepcion__activo=True)
            .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .order_by().values("orden_compra_detalle_id").annotate(t=Sum("cantidad_recibida")).values("t")[:1],
            output_field=num,
        ),
        Decimal("0"), output_field=num,
    )
    qs = OrdenCompraDetalle.objects.filter(
        producto_id__in=list(productos), orden_compra__empresa_id=empresa_id, orden_compra__activo=True,
        orden_compra__estatus__in=[
            OrdenCompra.EstatusOrdenCompra.AUTORIZADA, OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
        ],
    )
    if sucursal_id is not None:
        qs = qs.filter(orden_compra__sucursal_id=sucursal_id)
    out = defaultdict(lambda: CERO)
    for pid, cant, rec in qs.annotate(rec=recibido).values_list("producto_id", "cantidad", "rec"):
        out[pid] += max(Decimal(str(cant or 0)) - Decimal(str(rec or 0)), CERO)
    return dict(out)


def materiales(pre):
    """Por material: ``requerido`` (Σ BOM × piezas, con desperdicio, como el
    consumo de la OP), ``existencia``, ``en_transito``, ``comprometido`` (lo que
    piden las otras pre-órdenes abiertas de la sucursal), ``faltante`` =
    requerido − existencia y ``faltante_real`` = requerido − existencia −
    en tránsito. 6 consultas."""
    otras = list(
        PreOrdenProduccion.objects.filter(
            empresa_id=pre.empresa_id, sucursal_id=pre.sucursal_id, estatus__in=ABIERTAS,
        ).exclude(pk=pre.pk).values_list("pk", flat=True)
    )
    reqs = requeridos([pre.pk, *otras], pre.empresa_id)
    req = reqs.get(pre.pk, {})
    comprometido = defaultdict(lambda: CERO)
    for pid in otras:
        for prod, m in reqs.get(pid, {}).items():
            if prod in req:
                comprometido[prod] += m["requerido"]
    ex = existencias(pre.empresa_id, req, pre.sucursal_id)
    tr = en_transito(pre.empresa_id, req, pre.sucursal_id)
    out = []
    for m in sorted(req.values(), key=lambda x: x["nombre"] or ""):
        e = max(ex.get(m["producto_id"], CERO), CERO)
        t = tr.get(m["producto_id"], CERO)
        out.append({
            **m, "existencia": e, "en_transito": t, "comprometido": comprometido[m["producto_id"]],
            "faltante": max(m["requerido"] - e, CERO),
            "faltante_real": max(m["requerido"] - e - t, CERO),
        })
    return out


def _faltan(mats):
    return [m for m in mats if m["faltante"] > EPS]


# --------------------------------------------------------------------------- necesidades

def _necesidades_activas(pre):
    from compras.models import NecesidadCompra as N
    return N.objects.filter(pre_orden=pre, estatus__in=[N.Estatus.PENDIENTE, N.Estatus.EN_OC])


def _ajustar_necesidades(pre, mats, usuario=None):
    """Iguala las necesidades activas de la pre-orden a sus faltantes: crea las
    que faltan (o la diferencia), reduce las pendientes que sobran y, si ya
    están en una OC, deja el excedente en ``sobrante`` sin tocar la OC.
    Devuelve las necesidades creadas."""
    from compras.models import NecesidadCompra as N
    pendiente_por_prod = defaultdict(list)
    activo_por_prod = defaultdict(lambda: CERO)
    for n in _necesidades_activas(pre).order_by("-id"):
        neto = n.cantidad - n.sobrante
        activo_por_prod[n.producto_id] += neto
        pendiente_por_prod[n.producto_id].append(n)
    compromiso = fecha_compromiso(pre.pedido)
    creadas = []
    faltante = {m["producto_id"]: m for m in mats}
    for prod in set(faltante) | set(activo_por_prod):
        m = faltante.get(prod)
        falta = m["faltante"] if m else CERO
        diff = falta - activo_por_prod[prod]
        if diff > EPS:
            creadas.append(N.objects.create(
                empresa_id=pre.empresa_id, sucursal_id=pre.sucursal_id, producto_id=prod,
                unidad_id=m["unidad_id"], cantidad=diff.quantize(Q4), pre_orden=pre, pedido_id=pre.pedido_id,
                fecha_requerida=compromiso,
            ))
        elif diff < -EPS:
            sobra = -diff
            for n in pendiente_por_prod[prod]:  # primero las más nuevas
                if sobra <= EPS:
                    break
                neto = n.cantidad - n.sobrante
                quita = min(neto, sobra)
                if n.estatus == N.Estatus.PENDIENTE:
                    n.cantidad -= quita
                    if n.cantidad <= EPS:
                        n.estatus = N.Estatus.CANCELADA
                    n.save(update_fields=["cantidad", "estatus", "updated_at"])
                else:
                    n.sobrante += quita
                    n.save(update_fields=["sobrante", "updated_at"])
                sobra -= quita
    if creadas:
        evento(pre, EV.NECESIDAD_ENVIADA, usuario, "; ".join(
            f"{faltante[n.producto_id]['nombre']}: {n.cantidad.normalize():f} {faltante[n.producto_id]['unidad']}".strip()
            for n in creadas
        ))
    return creadas


def _estatus_segun(pre, mats):
    if not _faltan(mats):
        return E.LISTA
    return E.EN_COMPRA if _necesidades_activas(pre).exists() else E.FALTAN_MATERIALES


# --------------------------------------------------------------------------- flujo

def _tallas_con_bom(pedido_id):
    """``{variante_id: (pedido_detalle_id, cantidad)}``: tallas de muestra con
    cantidad > 0 cuya variante de producción activa tiene BOM activo con
    insumos. 2 consultas."""
    tallas = {
        (pd, t): c
        for pd, t, c in PedidoDetalleTalla.objects.filter(
            pedido_detalle__in=detalles_de_muestra().filter(pedido_id=pedido_id).values("pk"),
            cantidad__gt=0,
        ).values_list("pedido_detalle_id", "talla_id", "cantidad")
    }
    con_bom = BomDetalle.objects.filter(
        bom__variante_produccion_id=OuterRef("pk"), bom__activo=True, activo=True, componente__isnull=False,
    )
    out = {}
    for vid, pd, t in (
        VarianteProductoProduccion.objects.filter(activo=True, pedido_detalle__pedido_id=pedido_id)
        .filter(Exists(con_bom)).values_list("id", "pedido_detalle_id", "talla_id")
    ):
        cantidad = tallas.get((pd, t))
        if cantidad:
            out[vid] = (pd, Decimal(str(cantidad)))
    return out


@transaction.atomic
def sincronizar_pre_orden(pedido_id, usuario=None):
    """Crea o actualiza la pre-orden abierta del pedido con las tallas de muestra
    que ya tienen BOM. No crea una nueva si el pedido ya tiene una convertida
    en OP (para no duplicar producción). Si ya estaba validada, recalcula y
    ajusta sus necesidades de compra. Devuelve la pre-orden o ``None``."""
    pedido = Pedido.objects.select_for_update().filter(pk=pedido_id).only("id", "empresa_id", "sucursal_id").first()
    if pedido is None:
        return None
    deseadas = _tallas_con_bom(pedido.pk)
    pre = pre_orden_abierta(pedido.pk, lock=True)
    nueva = pre is None
    if nueva:
        if not deseadas or PreOrdenProduccion.objects.filter(pedido=pedido, estatus=E.CONVERTIDA).exists():
            return None
        pre = PreOrdenProduccion.objects.create(
            empresa_id=pedido.empresa_id, sucursal_id=pedido.sucursal_id, pedido=pedido,
            estatus_desde=timezone.now(),
        )
    actuales = {d.variante_produccion_id: d for d in pre.detalles.all()}
    quitar = set(actuales) - set(deseadas)
    PreOrdenProduccionDetalle.objects.filter(pre_orden=pre, variante_produccion_id__in=quitar).delete()
    nuevos, cambios = [], len(quitar)
    for vid, (pd, cantidad) in deseadas.items():
        d = actuales.get(vid)
        if d is None:
            nuevos.append(PreOrdenProduccionDetalle(
                pre_orden=pre, variante_produccion_id=vid, pedido_detalle_id=pd, cantidad=cantidad,
            ))
        elif d.cantidad != cantidad:
            d.cantidad = cantidad
            d.save(update_fields=["cantidad"])
            cambios += 1
    PreOrdenProduccionDetalle.objects.bulk_create(nuevos)
    if nueva:
        evento(pre, EV.CREADA, usuario, f"{len(nuevos)} talla(s) con BOM.")
    else:
        evento(pre, EV.BOM_EDITADO, usuario, f"{len(nuevos)} talla(s) nuevas, {cambios} modificada(s) o quitada(s).")
    mats = materiales(pre)
    if pre.estatus in RECALCULABLES:
        _ajustar_necesidades(pre, mats, usuario)
        recalcular(pre, mats, usuario=usuario)
    else:
        _guardar_faltantes(pre, mats)
    return pre


def _guardar_faltantes(pre, mats):
    n = len(_faltan(mats))
    if n != pre.materiales_faltantes:
        pre.materiales_faltantes = n
        pre.save(update_fields=["materiales_faltantes", "updated_at"])


def recalcular(pre, mats=None, motivo_regreso=None, usuario=None):
    """Mueve una pre-orden ya validada entre Faltan materiales / En compra /
    Lista para producir según las existencias de hoy y guarda el conteo de
    faltantes. Si estaba Lista y ya no alcanza, registra el ``regreso``."""
    mats = materiales(pre) if mats is None else mats
    faltantes = len(_faltan(mats))
    campos = []
    if faltantes != pre.materiales_faltantes:
        pre.materiales_faltantes = faltantes
        campos.append("materiales_faltantes")
    if pre.estatus in RECALCULABLES:
        antes = pre.estatus
        nuevo = _estatus_segun(pre, mats)
        campos = _cambiar_estatus(pre, nuevo, campos)
        if nuevo != antes and nuevo == E.LISTA:
            evento(pre, EV.LISTA, usuario, "Hay el 100% de los materiales.")
        if antes == E.LISTA and nuevo != E.LISTA:
            pre.regreso_fecha = timezone.now()
            pre.regreso_motivo = motivo_regreso or "Otra orden consumió el material."
            campos += ["regreso_fecha", "regreso_motivo"]
            evento(pre, EV.REGRESO_CONSUMO, usuario, pre.regreso_motivo + f" Faltan {faltantes} material(es).")
    if campos:
        pre.save(update_fields=[*campos, "updated_at"])
    return pre


def recalcular_ids(ids, motivo_regreso=None):
    for pre in PreOrdenProduccion.objects.filter(pk__in=list(ids), estatus__in=RECALCULABLES).select_related("pedido"):
        recalcular(pre, motivo_regreso=motivo_regreso)


def recalcular_listas(empresa_id, sucursal_id, op=None):
    """Tras consumir una OP: las pre-órdenes Listas de la sucursal pueden haberse
    quedado sin material."""
    motivo = f"La OP {op.folio_op} consumió el material." if op is not None else None
    recalcular_ids(
        PreOrdenProduccion.objects.filter(
            empresa_id=empresa_id, sucursal_id=sucursal_id, estatus=E.LISTA,
        ).exclude(op=op).values_list("pk", flat=True),
        motivo_regreso=motivo,
    )


def repartir_recepcion_oc(oc_id):
    """Tras recibir una OC: lo recibido de cada renglón se reparte entre sus
    necesidades por ``fecha_requerida`` (la más urgente primero) y se
    recalculan las pre-órdenes afectadas."""
    from compras.models import NecesidadCompra as N, OrdenCompra, Recepcion, RecepcionDetalle
    necs = list(
        N.objects.filter(orden_compra_id=oc_id, estatus__in=[N.Estatus.EN_OC, N.Estatus.RECIBIDA, N.Estatus.HUERFANA])
        .select_related("pre_orden", "producto")
        .order_by(models.F("fecha_requerida").asc(nulls_last=True), "id")
    )
    oc = OrdenCompra.objects.filter(pk=oc_id).values("folio", "pre_orden_id").first() or {}
    afectadas = set()
    if necs:
        recibido = {
            d: Decimal(str(t or 0))
            for d, t in RecepcionDetalle.objects.filter(
                orden_compra_detalle_id__in={n.orden_compra_detalle_id for n in necs}, recepcion__activo=True,
            ).exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .values("orden_compra_detalle_id").annotate(t=Sum("cantidad_recibida")).values_list("orden_compra_detalle_id", "t")
        }
        por_renglon = defaultdict(list)
        for n in necs:
            por_renglon[n.orden_compra_detalle_id].append(n)
        for det_id, lista in por_renglon.items():
            resto = recibido.get(det_id, CERO)
            for n in lista:
                asignado = min(max(resto, CERO), n.cantidad)
                resto -= asignado
                nuevo_est = n.estatus
                if n.estatus != N.Estatus.HUERFANA:
                    nuevo_est = N.Estatus.RECIBIDA if asignado >= n.cantidad - EPS else N.Estatus.EN_OC
                if asignado != n.cantidad_recibida or nuevo_est != n.estatus:
                    nuevo_recibido = asignado - n.cantidad_recibida
                    n.cantidad_recibida, n.estatus = asignado, nuevo_est
                    n.save(update_fields=["cantidad_recibida", "estatus", "updated_at"])
                    if nuevo_recibido > EPS and n.estatus != N.Estatus.HUERFANA:
                        evento(n.pre_orden, EV.MATERIAL_RECIBIDO, None,
                               f"{n.producto.nombre}: {nuevo_recibido.normalize():f} (OC {oc.get('folio') or oc_id}).")
                        afectadas.add(n.pre_orden_id)
    if oc.get("pre_orden_id"):  # OC de la versión anterior (una por pre-orden)
        afectadas.add(oc["pre_orden_id"])
    recalcular_ids(afectadas)


def eventos_oc(oc_id, tipo, usuario=None):
    """``oc_creada``/``oc_autorizada`` en cada pre-orden con necesidades en la OC."""
    from compras.models import NecesidadCompra as N, OrdenCompra
    oc = OrdenCompra.objects.filter(pk=oc_id).select_related("proveedor").first()
    if oc is None:
        return
    for pre in PreOrdenProduccion.objects.filter(necesidades__orden_compra_id=oc_id).distinct():
        evento(pre, tipo, usuario, f"OC {oc.folio or oc.pk}" + (f" con {oc.proveedor.nombre}" if oc.proveedor_id else "") + ".")


@transaction.atomic
def confirmar_existencias(pre_id, user):
    """Almacén confirma. Sin faltantes → Lista para producir. Con faltantes →
    una necesidad de compra por material (sin duplicar las activas) → En compra."""
    pre = PreOrdenProduccion.objects.select_for_update().select_related("empresa", "pedido").get(pk=pre_id)
    if pre.estatus not in ABIERTAS:
        raise ValidationError({"estatus": f"La pre-orden está {pre.get_estatus_display().lower()}."})
    if not pre.detalles.exists():
        raise ValidationError({"detalles": "La pre-orden no tiene tallas con BOM."})
    mats = materiales(pre)
    creadas = _ajustar_necesidades(pre, mats, user)
    faltan = _faltan(mats)
    evento(pre, EV.EXISTENCIAS_CONFIRMADAS, user,
           f"{len(faltan)} material(es) con faltante." if faltan else "Sin faltantes.")
    antes = pre.estatus
    campos = _cambiar_estatus(pre, _estatus_segun(pre, mats))
    pre.materiales_faltantes = len(faltan)
    pre.fecha_confirmacion_existencias = timezone.now()
    pre.usuario_confirma = user
    pre.save(update_fields=[*campos, "materiales_faltantes", "fecha_confirmacion_existencias", "usuario_confirma", "updated_at"])
    if pre.estatus == E.LISTA and antes != E.LISTA:
        evento(pre, EV.LISTA, user, "Hay el 100% de los materiales.")
    return pre, mats, creadas


@transaction.atomic
def cancelar(pre_id, user):
    """Cancela y libera necesidades: las pendientes se cancelan; las que ya
    están en una OC quedan ``huerfana`` para que compras decida."""
    from compras.models import NecesidadCompra as N
    pre = PreOrdenProduccion.objects.select_for_update().get(pk=pre_id)
    if pre.estatus not in ABIERTAS:
        raise ValidationError({"estatus": f"La pre-orden está {pre.get_estatus_display().lower()}."})
    liberadas = N.objects.filter(pre_orden=pre, estatus=N.Estatus.PENDIENTE).update(estatus=N.Estatus.CANCELADA, updated_at=timezone.now())
    huerfanas = N.objects.filter(pre_orden=pre, estatus=N.Estatus.EN_OC).update(estatus=N.Estatus.HUERFANA, updated_at=timezone.now())
    campos = _cambiar_estatus(pre, E.CANCELADA)
    pre.save(update_fields=[*campos, "updated_at"])
    evento(pre, EV.CANCELADA, user, f"{liberadas} necesidad(es) liberada(s), {huerfanas} huérfana(s) en OC.")
    return pre


def marcar_convertida(pre, op, user):
    campos = _cambiar_estatus(pre, E.CONVERTIDA)
    pre.op = op
    pre.save(update_fields=[*campos, "op", "updated_at"])
    evento(pre, EV.OP_CREADA, user, f"OP {op.folio_op}.")
