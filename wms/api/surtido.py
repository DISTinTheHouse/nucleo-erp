"""Avance de surtido por pedido: ``/api/v1/wms/surtido-pedidos/``.

Una sola vista informativa (solo lectura) de lo que cada pedido lleva en WMS.

Reglas (acordadas con operación):

- **Surtido** = lo que WMS ya surtió por picking (``PickingDetalle.cantidad_surtida``
  de pickings y renglones no cancelados) **con talla del pedido**: un renglón de
  picking sin ``pedido_detalle_talla`` no cuenta, igual que en el listado de
  pedidos, ``tracker_picking`` y ``disponibilidad`` (#1246). Lo asignado cuenta
  como surtido desde que se crea el picking: la TR ya sacó la prenda del almacén.
- **Comprometido** = pedido − surtido **por talla**, nunca negativo; el del pedido
  es la suma de sus tallas, así que una talla sobre-surtida no compensa a otra
  (#1247): encabezado = Σ renglones. Deja de estar comprometido al hacer picking.
- **Empacado** y **despachado** son etapas informativas posteriores; el despacho
  da salida al inventario (``MovimientoInventario.despacho``).
- **Salidas manuales**: neto de salidas menos entradas de inventario ligadas al
  pedido **fuera de WMS**: movimientos activos, sin OP, sin despacho, y entradas
  que no son de recepción ni transferencia (#1249, #1250); nunca negativo. Solo
  informativas: NO cuentan como surtido, para no contar doble.

Listado: ``?estatus=3,4`` (por omisión autorizados y en proceso; ``todos`` para
cualquiera), ``?q=`` (folio o cliente), ``?sucursal=``, ``?pendientes=1`` (con
comprometido > 0). Una consulta por página, sin importar los pedidos.
Detalle: ``/{pedido_id}/`` con los mismos totales y un renglón por talla.
"""
from decimal import Decimal

from django.db.models import Case, DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Cast, Coalesce, Greatest
from rest_framework import mixins
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from inventarios.models import MovimientoInventarioDetalle
from ventas.models import Pedido, PedidoDetalleTalla
from wms.models import DespachoDetalle, Picking, PickingDetalle, PackingDetalle
from wms.scope import documentos_wms_visibles

NUM = DecimalField(max_digits=18, decimal_places=4)
CERO = Value(Decimal("0"), output_field=NUM)


def _suma(qs, campo, llave):
    """Subconsulta ``Σ campo`` agrupada por ``llave`` = OuterRef("pk")."""
    return Coalesce(
        Subquery(qs.filter(**{llave: OuterRef("pk")}).order_by().values(llave)
                 .annotate(t=Sum(campo)).values("t")[:1], output_field=NUM),
        CERO, output_field=NUM,
    )


def _picking_vivo():
    # Solo renglones con talla del pedido (#1246), como las otras fuentes de surtido.
    return PickingDetalle.objects.filter(pedido_detalle_talla__isnull=False).exclude(
        picking__estado=Picking.Estado.CANCELADO).exclude(estado=PickingDetalle.EstadoLinea.CANCELADA)


def _packing_vivo():
    return PackingDetalle.objects.filter(picking_detalle__pedido_detalle_talla__isnull=False).exclude(
        estado="CANCELADO").exclude(packing__estado="CANCELADO")


def _faltante_por_talla(fuente, campo, llave):
    """Σ por talla de ``max(pedida − Σ campo de fuente, 0)`` del pedido ``OuterRef("pk")``:
    una talla sobrada no compensa a otra (#1247, #1290)."""
    tallas = (
        PedidoDetalleTalla.objects.filter(pedido_detalle__pedido_id=OuterRef("pk"))
        .annotate(s=_suma(fuente, campo, llave))
        .annotate(c=Greatest(F("cantidad") - F("s"), CERO, output_field=NUM))
        .order_by().values("pedido_detalle__pedido_id").annotate(t=Sum("c")).values("t")[:1]
    )
    return Coalesce(Subquery(tallas, output_field=NUM), CERO, output_field=NUM)


def _comprometidas_por_talla():
    """Σ por talla de ``max(pedida − surtida, 0)`` del pedido (#1247)."""
    return _faltante_por_talla(_picking_vivo(), "cantidad_surtida", "pedido_detalle_talla_id")


def _por_despachar_por_talla():
    """Σ por talla de ``max(pedida − despachada, 0)`` del pedido (#1290)."""
    return _faltante_por_talla(
        _despachado(_packing_vivo()), "cantidad_empacada", "picking_detalle__pedido_detalle_talla_id",
    )


def _salidas_manuales():
    """Neto SALIDA − ENTRADA fuera de WMS del pedido ``OuterRef("pk")`` (#1249, #1250)."""
    S, E = "SALIDA", "ENTRADA"
    movs = (
        MovimientoInventarioDetalle.objects.filter(
            movimiento_inventario__pedido_id=OuterRef("pk"), movimiento_inventario__activo=True,
            movimiento_inventario__op__isnull=True, movimiento_inventario__despacho__isnull=True,
        )
        .filter(Q(movimiento_inventario__tipo_movimiento=S) | Q(
            movimiento_inventario__tipo_movimiento=E, movimiento_inventario__recepcion__isnull=True,
            movimiento_inventario__transferencia__isnull=True))
        .order_by().values("movimiento_inventario__pedido_id")
        .annotate(t=Sum(Case(
            When(movimiento_inventario__tipo_movimiento=S, then=F("cantidad")),
            default=-F("cantidad"), output_field=NUM,
        ))).values("t")[:1]
    )
    # Piso en 0: una entrada sin salida previa (p. ej. una devolución) no da negativo.
    neto = Greatest(Coalesce(Subquery(movs, output_field=NUM), CERO, output_field=NUM), CERO, output_field=NUM)
    return Cast(neto, NUM)  # 4 decimales, como los demás totales


def _despachado(qs):
    return qs.filter(Exists(DespachoDetalle.objects.filter(packing_detalle_id=OuterRef("pk"), despacho__estado="ACTIVO")))


def anotar_avance(qs, nivel):
    """Anota ``pedidas/surtidas/empacadas/despachadas/comprometidas`` (y
    ``salidas_manuales`` por pedido). ``nivel``: ``"pedido"`` o ``"talla"``."""
    if nivel == "pedido":
        llaves = {"t": "pedido_detalle__pedido_id", "pk": "pedido_detalle__pedido_id",
                  "pack": "picking_detalle__pedido_detalle__pedido_id"}
        pedidas = _suma(PedidoDetalleTalla.objects.all(), "cantidad", llaves["t"])
    else:
        llaves = {"pk": "pedido_detalle_talla_id", "pack": "picking_detalle__pedido_detalle_talla_id"}
        pedidas = F("cantidad")
    qs = qs.annotate(
        pedidas=Coalesce(pedidas, CERO, output_field=NUM),
        surtidas=_suma(_picking_vivo(), "cantidad_surtida", llaves["pk"]),
        empacadas=_suma(_packing_vivo(), "cantidad_empacada", llaves["pack"]),
        despachadas=_suma(_despachado(_packing_vivo()), "cantidad_empacada", llaves["pack"]),
    )
    # ``por_despachar`` solo decide la etapa (no sale en la respuesta).
    if nivel == "pedido":
        qs = qs.annotate(comprometidas=_comprometidas_por_talla(), por_despachar=_por_despachar_por_talla(),
                         salidas_manuales=_salidas_manuales())
    else:
        qs = qs.annotate(comprometidas=Greatest(F("pedidas") - F("surtidas"), CERO, output_field=NUM),
                         por_despachar=Greatest(F("pedidas") - F("despachadas"), CERO, output_field=NUM))
    return qs


def _pct(parte, total):
    return round(float(parte) * 100 / float(total), 2) if total else 0.0


def _totales(o):
    p = o.pedidas
    return {
        "pedidas": p, "surtidas": o.surtidas, "comprometidas": o.comprometidas,
        "empacadas": o.empacadas, "despachadas": o.despachadas,
        "pct_surtido": _pct(o.surtidas, p), "pct_empacado": _pct(o.empacadas, p),
        "pct_despachado": _pct(o.despachadas, p),
    }


def _etapa(o):
    """Por talla, no por totales (#1290): con una talla sobre-surtida y otra corta el
    pedido sigue PARCIAL (``comprometidas > 0``), aunque Σ surtidas ≥ Σ pedidas."""
    if o.pedidas and o.por_despachar <= 0:
        return "DESPACHADO"
    if o.pedidas and o.comprometidas <= 0:
        return "SURTIDO"
    if o.surtidas > 0:
        return "PARCIAL"
    return "SIN_SURTIR"


def _fila_pedido(o):
    return {
        "id": o.pk, "folio": o.folio, "cliente_nombre": o.cliente_nombre, "sucursal_id": o.sucursal_id,
        "estatus": o.estatus, "estatus_display": o.get_estatus_display(),
        "fecha_confirmacion": o.fecha_confirmacion, "created_at": o.created_at,
        "etapa": _etapa(o), **_totales(o), "salidas_manuales": o.salidas_manuales,
    }


class _Pagina(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class SurtidoPedidoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    pagination_class = _Pagina

    def get_queryset(self):
        qs = documentos_wms_visibles(Pedido.objects.filter(activo=True), self.request.user)
        if self.action == "list":
            p = self.request.query_params
            estatus = (p.get("estatus") or "3,4").strip().lower()
            if estatus != "todos":
                try:
                    qs = qs.filter(estatus__in=[int(x) for x in estatus.split(",") if x.strip()])
                except ValueError:
                    raise ValidationError({"estatus": "Lista de enteros (p. ej. 3,4) o 'todos'."})
            if p.get("sucursal"):
                # ``isdecimal`` + ASCII: ``"²".isdigit()`` es True y ``int("²")`` falla (500, #1252).
                if not (str(p["sucursal"]).isascii() and str(p["sucursal"]).isdecimal()):
                    raise ValidationError({"sucursal": "Debe ser un número entero."})
                qs = qs.filter(sucursal_id=int(p["sucursal"]))
            if p.get("q"):
                q = p["q"].strip()
                qs = qs.filter(Q(folio__icontains=q) | Q(cliente_nombre__icontains=q))
            qs = anotar_avance(qs, "pedido")
            if p.get("pendientes") in ("1", "true"):
                qs = qs.filter(comprometidas__gt=0)
        else:
            qs = anotar_avance(qs, "pedido")
        return qs.only("id", "folio", "cliente_nombre", "sucursal_id", "estatus", "fecha_confirmacion",
                       "created_at", "empresa_id").order_by("-id")

    def list(self, request, *args, **kwargs):
        pagina = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([_fila_pedido(o) for o in pagina])

    def retrieve(self, request, *args, **kwargs):
        pedido = self.get_object()
        tallas = anotar_avance(
            PedidoDetalleTalla.objects.filter(pedido_detalle__pedido_id=pedido.pk)
            .select_related("talla", "variante", "pedido_detalle__producto", "pedido_detalle__color"),
            "talla",
        ).order_by("pedido_detalle_id", "id")
        renglones = []
        for t in tallas:
            det = t.pedido_detalle
            renglones.append({
                "pedido_detalle_talla_id": t.pk, "pedido_detalle_id": det.pk,
                # Muestras sin producto de catálogo: el nombre capturado (#1248).
                "producto_id": det.producto_id,
                "producto_nombre": getattr(det.producto, "nombre", None) or det.producto_nombre_externo,
                "producto_nombre_externo": det.producto_nombre_externo,
                "sku": getattr(t.variante, "sku", None), "talla": getattr(t.talla, "nombre", None),
                "color": getattr(getattr(det, "color", None), "nombre", None),
                "etapa": _etapa(t), **_totales(t),
            })
        return Response({**_fila_pedido(pedido), "renglones": renglones})
