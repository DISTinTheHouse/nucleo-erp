"""Pre-orden de producción de Fabricación especial.

Flujo: el BOM de una variante de producción de muestra crea/actualiza la
pre-orden del pedido (``sincronizar_pre_orden``) → almacén confirma existencias
(``confirmar_existencias``; con faltantes se crea una OC en Borrador ligada) →
``recalcular`` la mueve entre Faltan materiales / En compra / Lista para producir
al recibir la OC o al consumir otra OP → ``crear-op`` la convierte.

No aparta material: la existencia es la de hoy en los almacenes de la sucursal
de la pre-orden (el mismo alcance con el que ``OrdenProduccionService`` descuenta).
"""
import math
from decimal import Decimal, ROUND_HALF_UP

from django.db import models, transaction
from django.db.models import Exists, OuterRef, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from catalogo.models import VarianteProductoProduccion
from inventarios.models import Existencia
from produccion.models import BomDetalle, PreOrdenProduccion, PreOrdenProduccionDetalle
from ventas.models import Pedido, PedidoDetalleTalla
from ventas.services.sku_produccion_service import detalles_de_muestra

E = PreOrdenProduccion.Estatus
ABIERTAS = (E.PENDIENTE_VALIDAR, E.FALTAN_MATERIALES, E.EN_COMPRA, E.LISTA)
# Estatus que se recalculan solos (ya validados por almacén).
RECALCULABLES = (E.FALTAN_MATERIALES, E.EN_COMPRA, E.LISTA)
Q4 = Decimal("0.0001")
EPS = Decimal("0.0001")


def pre_orden_abierta(pedido_id, lock=False):
    qs = PreOrdenProduccion.objects.filter(pedido_id=pedido_id, estatus__in=ABIERTAS)
    if lock:
        qs = qs.select_for_update()
    return qs.first()


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
def sincronizar_pre_orden(pedido_id):
    """Crea o actualiza la pre-orden abierta del pedido con las tallas de muestra
    que ya tienen BOM. No crea una nueva si el pedido ya tiene una convertida
    en OP (para no duplicar producción). Devuelve la pre-orden o ``None``."""
    pedido = Pedido.objects.select_for_update().filter(pk=pedido_id).only("id", "empresa_id", "sucursal_id").first()
    if pedido is None:
        return None
    deseadas = _tallas_con_bom(pedido.pk)
    pre = pre_orden_abierta(pedido.pk, lock=True)
    if pre is None:
        if not deseadas or PreOrdenProduccion.objects.filter(pedido=pedido, estatus=E.CONVERTIDA).exists():
            return None
        pre = PreOrdenProduccion.objects.create(
            empresa_id=pedido.empresa_id, sucursal_id=pedido.sucursal_id, pedido=pedido,
        )
    actuales = {d.variante_produccion_id: d for d in pre.detalles.all()}
    PreOrdenProduccionDetalle.objects.filter(
        pre_orden=pre, variante_produccion_id__in=set(actuales) - set(deseadas)
    ).delete()
    nuevos = []
    for vid, (pd, cantidad) in deseadas.items():
        d = actuales.get(vid)
        if d is None:
            nuevos.append(PreOrdenProduccionDetalle(
                pre_orden=pre, variante_produccion_id=vid, pedido_detalle_id=pd, cantidad=cantidad,
            ))
        elif d.cantidad != cantidad:
            d.cantidad = cantidad
            d.save(update_fields=["cantidad"])
    PreOrdenProduccionDetalle.objects.bulk_create(nuevos)
    if pre.estatus in RECALCULABLES:
        recalcular(pre)
    return pre


def materiales(pre):
    """Por material: ``requerido`` (Σ BOM × piezas, con desperdicio, igual que
    el consumo de la OP), ``existencia`` (almacenes de la sucursal) y
    ``faltante``. 3 consultas."""
    piezas = dict(pre.detalles.values_list("variante_produccion_id", "cantidad"))
    filas = list(
        BomDetalle.objects.filter(
            bom__variante_produccion_id__in=list(piezas), bom__activo=True, bom__empresa_id=pre.empresa_id,
            activo=True, componente__isnull=False,
        ).values(
            "bom_id", "bom__variante_produccion_id", "componente_id", "componente__nombre",
            "componente__codigo", "componente__cod_proscai", "unidad__clave", "cantidad", "desperdicio",
        )
    )
    # BOM vigente por variante: el más reciente activo (mismo criterio que el onboarding de OP).
    vigente = {}
    for f in filas:
        v = f["bom__variante_produccion_id"]
        vigente[v] = max(vigente.get(v, 0), f["bom_id"])
    req = {}
    for f in filas:
        v = f["bom__variante_produccion_id"]
        if f["bom_id"] != vigente[v]:
            continue
        factor = Decimal("1") + Decimal(str(f["desperdicio"] or 0)) / Decimal("100")
        cant = (Decimal(str(piezas[v])) * Decimal(str(f["cantidad"] or 0)) * factor).quantize(Q4, ROUND_HALF_UP)
        m = req.setdefault(f["componente_id"], {
            "producto_id": f["componente_id"],
            "codigo": f["componente__cod_proscai"] or f["componente__codigo"] or "",
            "nombre": f["componente__nombre"],
            "unidad": (f["unidad__clave"] or "").upper(),
            "requerido": Decimal("0"),
        })
        m["requerido"] += cant
    existencias = {}
    if req:
        for pid, total in (
            Existencia.objects.filter(almacen__empresa_id=pre.empresa_id, almacen__sucursal_id=pre.sucursal_id)
            .filter(models.Q(producto_id__in=list(req)) | models.Q(producto_variante__producto_id__in=list(req)))
            .annotate(pid=Coalesce("producto_id", "producto_variante__producto_id"))
            .values("pid").annotate(t=Sum("cantidad")).values_list("pid", "t")
        ):
            existencias[pid] = Decimal(str(total or 0))
    out = []
    for m in sorted(req.values(), key=lambda x: x["nombre"] or ""):
        ex = existencias.get(m["producto_id"], Decimal("0"))
        falt = max(m["requerido"] - max(ex, Decimal("0")), Decimal("0"))
        out.append({**m, "existencia": ex, "faltante": falt})
    return out


def _oc_abierta(pre):
    from compras.models import OrdenCompra
    return OrdenCompra.objects.filter(pre_orden=pre, activo=True).exclude(
        estatus__in=[OrdenCompra.EstatusOrdenCompra.RECIBIDA, OrdenCompra.EstatusOrdenCompra.CANCELADA]
    ).first()


def _estatus_segun(pre, mats):
    if not any(m["faltante"] > EPS for m in mats):
        return E.LISTA
    return E.EN_COMPRA if _oc_abierta(pre) else E.FALTAN_MATERIALES


def recalcular(pre, mats=None):
    """Mueve una pre-orden ya validada entre Faltan materiales / En compra /
    Lista para producir según las existencias de hoy."""
    if pre.estatus not in RECALCULABLES:
        return pre
    nuevo = _estatus_segun(pre, mats if mats is not None else materiales(pre))
    if nuevo != pre.estatus:
        pre.estatus = nuevo
        pre.save(update_fields=["estatus", "updated_at"])
    return pre


def recalcular_ids(ids):
    for pre in PreOrdenProduccion.objects.filter(pk__in=list(ids), estatus__in=RECALCULABLES):
        recalcular(pre)


def recalcular_por_oc(oc_id):
    """Tras recibir una OC: recalcula su pre-orden (si la tiene)."""
    from compras.models import OrdenCompra
    pid = OrdenCompra.objects.filter(pk=oc_id).values_list("pre_orden_id", flat=True).first()
    if pid:
        recalcular_ids([pid])


def recalcular_listas(empresa_id, sucursal_id):
    """Tras consumir una OP: las pre-órdenes Listas de la sucursal pueden haberse
    quedado sin material."""
    recalcular_ids(
        PreOrdenProduccion.objects.filter(
            empresa_id=empresa_id, sucursal_id=sucursal_id, estatus=E.LISTA,
        ).values_list("pk", flat=True)
    )


def _crear_oc_faltantes(pre, mats, user):
    from compras.models import OrdenCompra, OrdenCompraDetalle
    from nucleo.models import Moneda
    empresa = pre.empresa
    moneda = (
        getattr(empresa, "moneda_base", None)
        or Moneda.objects.filter(empresa__isnull=True, codigo_iso="MXN", activo=True).first()
        or Moneda.objects.filter(empresa__isnull=True, activo=True).order_by("codigo_iso").first()
    )
    faltantes = [m for m in mats if m["faltante"] > EPS]
    piezas = [max(int(math.ceil(m["faltante"])), 1) for m in faltantes]
    oc = OrdenCompra.objects.create(
        empresa=empresa, sucursal_id=pre.sucursal_id, moneda=moneda, usuario=user,
        pedido_id=pre.pedido_id, pre_orden=pre,
        tipo_compra=OrdenCompra.TipoCompra.MATERIA_PRIMA,
        estatus=OrdenCompra.EstatusOrdenCompra.BORRADOR,
        fecha_oc=timezone.localdate(), total_piezas=sum(piezas),
        observaciones=f"Faltantes de la pre-orden de producción {pre.pk} (pedido {pre.pedido.folio}).",
    )
    OrdenCompraDetalle.objects.bulk_create([
        OrdenCompraDetalle(
            orden_compra=oc, producto_id=m["producto_id"], descripcion=(m["nombre"] or "")[:200],
            cantidad=n, piezas=n, sucursal_id=pre.sucursal_id,
        )
        for m, n in zip(faltantes, piezas)
    ])
    return oc


@transaction.atomic
def confirmar_existencias(pre_id, user):
    """Almacén confirma. Sin faltantes → Lista para producir. Con faltantes →
    OC en Borrador solo con ellos (si no hay ya una abierta) → En compra."""
    pre = PreOrdenProduccion.objects.select_for_update().select_related("empresa", "pedido").get(pk=pre_id)
    if pre.estatus not in ABIERTAS:
        raise ValidationError({"estatus": f"La pre-orden está {pre.get_estatus_display().lower()}."})
    if not pre.detalles.exists():
        raise ValidationError({"detalles": "La pre-orden no tiene tallas con BOM."})
    mats = materiales(pre)
    oc = None
    if any(m["faltante"] > EPS for m in mats) and _oc_abierta(pre) is None:
        oc = _crear_oc_faltantes(pre, mats, user)
    pre.estatus = _estatus_segun(pre, mats)
    pre.fecha_confirmacion_existencias = timezone.now()
    pre.usuario_confirma = user
    pre.save(update_fields=["estatus", "fecha_confirmacion_existencias", "usuario_confirma", "updated_at"])
    return pre, mats, oc
