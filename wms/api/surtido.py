"""Avance de surtido por pedido: ``/api/v1/wms/surtido-pedidos/``.

Una sola vista informativa (solo lectura) de lo que cada pedido lleva en WMS.

Reglas (acordadas con operación):

- **Surtido** = lo que WMS ya surtió por picking (``PickingDetalle.cantidad_surtida``
  de pickings y renglones no cancelados). Lo asignado cuenta como surtido desde
  que se crea el picking: la TR ya sacó la prenda del almacén origen.
- **Comprometido** = pedido − surtido (nunca negativo): lo que el pedido todavía
  le aparta al stock libre. Deja de estar comprometido al hacer picking.
- **Empacado** y **despachado** son etapas informativas posteriores (packing y
  despacho no mueven inventario).
- **Salidas manuales**: salidas de inventario ligadas al pedido fuera de WMS (sin
  OP). Solo informativas: NO cuentan como surtido, para no contar doble.

Listado: ``?estatus=3,4`` (por omisión autorizados y en proceso; ``todos`` para
cualquiera), ``?q=`` (folio o cliente), ``?sucursal=``, ``?pendientes=1`` (con
comprometido > 0). Una consulta por página, sin importar los pedidos.
Detalle: ``/{pedido_id}/`` con los mismos totales y un renglón por talla.
"""
from decimal import Decimal

from django.db.models import DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest
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
    return PickingDetalle.objects.exclude(picking__estado=Picking.Estado.CANCELADO).exclude(
        estado=PickingDetalle.EstadoLinea.CANCELADA)


def _packing_vivo():
    return PackingDetalle.objects.exclude(estado="CANCELADO").exclude(packing__estado="CANCELADO")


def _despachado(qs):
    return qs.filter(Exists(DespachoDetalle.objects.filter(packing_detalle_id=OuterRef("pk"))))


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
    ).annotate(comprometidas=Greatest(F("pedidas") - F("surtidas"), CERO, output_field=NUM))
    if nivel == "pedido":
        qs = qs.annotate(salidas_manuales=_suma(
            MovimientoInventarioDetalle.objects.filter(
                movimiento_inventario__tipo_movimiento="SALIDA", movimiento_inventario__op__isnull=True),
            "cantidad", "movimiento_inventario__pedido_id"))
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
    if o.pedidas and o.despachadas >= o.pedidas:
        return "DESPACHADO"
    if o.pedidas and o.surtidas >= o.pedidas:
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
                if not str(p["sucursal"]).isdigit():
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
                "producto_id": det.producto_id, "producto_nombre": getattr(det.producto, "nombre", None),
                "sku": getattr(t.variante, "sku", None), "talla": getattr(t.talla, "nombre", None),
                "color": getattr(getattr(det, "color", None), "nombre", None),
                "etapa": _etapa(t), **_totales(t),
            })
        return Response({**_fila_pedido(pedido), "renglones": renglones})
