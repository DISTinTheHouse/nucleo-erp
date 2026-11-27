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

EC-988 (pestañas "Por surtir" y "Surtidos" de Pedidos WMS en modo servidor):

- ``?por_surtir=1``: lo que el onboarding de picking ofrece
  (``pedidos_por_surtir_q``, la misma expresión); fija el estatus en 3,4 e ignora
  ``?estatus=``. Cada fila trae además ``sucursal_nombre`` y ``programaciones``
  (la forma del onboarding), en bloque por página.
- ``?completo=1``: surtido completo, ``pedidas > 0`` y ``comprometidas = 0``.
- ``?destino=`` (alguna programación a ese destino), ``?etapa=`` (la de
  ``_etapa``, en SQL), uno o varios separados por coma; ``?cliente=<id>``.
- ``?ordering=`` sobre ``CAMPOS_ORDEN`` (desempate por ``id``); sin él, ``-id``.
- ``filtros/``: clientes y sucursales del conjunto (``estatus``, ``por_surtir``,
  ``completo``, ``pendientes``), sin los demás filtros.
- ``resumen/``: ``por_surtir`` y ``surtidos`` (los números de las pestañas), sin
  parámetros, en una consulta.
"""
from decimal import Decimal

from collections import defaultdict

from django.db.models import (
    BooleanField, Case, CharField, Count, DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value, When,
)
from django.db.models.functions import Cast, Coalesce, Greatest
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from inventarios.models import MovimientoInventarioDetalle
from nucleo.listados import OrdenamientoNullsAlFinal
from ventas.models import Pedido, PedidoDetalleTalla
from wms.api.pickings_listado import _valores_param
from wms.models import DespachoDetalle, Picking, PickingDetalle, PackingDetalle
from wms.scope import documentos_wms_visibles
from wms.services.picking_pipeline.catalogs import (
    almacenes_por_destino_de_sucursales, programaciones_de, surtido_por_destino_de_pedidos,
)
from wms.services.picking_pipeline.pendientes import pedidos_por_surtir_q

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


def _pedidas_pedido():
    """Σ ``cantidad`` de las tallas del pedido ``OuterRef("pk")``."""
    return _suma(PedidoDetalleTalla.objects.all(), "cantidad", "pedido_detalle__pedido_id")


def anotar_avance(qs, nivel):
    """Anota ``pedidas/surtidas/empacadas/despachadas/comprometidas`` (y
    ``salidas_manuales`` por pedido). ``nivel``: ``"pedido"`` o ``"talla"``."""
    if nivel == "pedido":
        llaves = {"pk": "pedido_detalle__pedido_id", "pack": "picking_detalle__pedido_detalle__pedido_id"}
        pedidas = _pedidas_pedido()
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


# --- EC-988: filtros del listado en SQL ------------------------------------------

ETAPA_DESPACHADO, ETAPA_SURTIDO, ETAPA_PARCIAL, ETAPA_SIN_SURTIR = "DESPACHADO", "SURTIDO", "PARCIAL", "SIN_SURTIR"
ETAPAS = (ETAPA_SIN_SURTIR, ETAPA_PARCIAL, ETAPA_SURTIDO, ETAPA_DESPACHADO)

# ``_etapa`` en SQL, rama por rama y en el mismo orden (``pedidas`` nunca es negativo:
# ``if o.pedidas`` es ``pedidas > 0``). Sólo se anota cuando llega ``?etapa=``.
ETAPA_SQL = Case(
    When(Q(pedidas__gt=0, por_despachar__lte=0), then=Value(ETAPA_DESPACHADO)),
    When(Q(pedidas__gt=0, comprometidas__lte=0), then=Value(ETAPA_SURTIDO)),
    When(surtidas__gt=0, then=Value(ETAPA_PARCIAL)),
    default=Value(ETAPA_SIN_SURTIR),
    output_field=CharField(),
)

# Surtido completo (pestaña "Surtidos", ``isSurtidoCompleto`` del front): algo pedido
# y nada comprometido. ``comprometidas`` es Σ de términos ≥ 0, así que = 0 ≡ ≤ 0.
COMPLETO_Q = Q(pedidas__gt=0, comprometidas=0)

DESTINOS = tuple(Picking.ProgramacionDestino.values)

# ``?ordering=``: columnas simples del pedido, nunca cifras anotadas (EC-988).
CAMPOS_ORDEN = ("id", "folio", "cliente_nombre", "created_at", "fecha_confirmacion")

_CAMPOS_FILA = ("id", "folio", "cliente_nombre", "sucursal_id", "estatus", "fecha_confirmacion",
                "created_at", "empresa_id")


def _bandera(params, nombre):
    return params.get(nombre) in ("1", "true")


def _id_param(params, nombre):
    """``?{nombre}=<id>`` → ``int`` o ``None``. ``isdecimal`` + ASCII: ``"²".isdigit()``
    es True y ``int("²")`` falla (500, #1252)."""
    valor = str(params.get(nombre) or "").strip()
    if not valor:
        return None
    if not (valor.isascii() and valor.isdecimal()):
        raise ValidationError({nombre: "Debe ser un número entero."})
    return int(valor)


def poblacion(qs, params):
    """El conjunto que se lista: ``estatus`` (o ``por_surtir``, que lo fija en 3,4),
    las cifras de avance y ``pendientes``/``completo``. Lo comparten el listado y
    ``filtros/``; los filtros de columna van aparte (``filtrar``)."""
    if _bandera(params, "por_surtir"):
        qs = qs.filter(pedidos_por_surtir_q())
    else:
        estatus = (params.get("estatus") or "3,4").strip().lower()
        if estatus != "todos":
            try:
                qs = qs.filter(estatus__in=[int(x) for x in estatus.split(",") if x.strip()])
            except ValueError:
                raise ValidationError({"estatus": "Lista de enteros (p. ej. 3,4) o 'todos'."})
    qs = anotar_avance(qs, "pedido")
    if _bandera(params, "pendientes"):
        qs = qs.filter(comprometidas__gt=0)
    if _bandera(params, "completo"):
        qs = qs.filter(COMPLETO_Q)
    return qs


def filtrar(request, qs):
    """Filtros de columna del listado: ``sucursal``, ``cliente``, ``q`` (folio o
    cliente), ``destino`` y ``etapa`` (uno o varios separados por coma; uno fuera de
    catálogo → 400, como ``pickings_listado``)."""
    params = request.query_params
    for nombre in ("sucursal", "cliente"):
        valor = _id_param(params, nombre)
        if valor is not None:
            qs = qs.filter(**{f"{nombre}_id": valor})
    if params.get("q"):
        q = params["q"].strip()
        qs = qs.filter(Q(folio__icontains=q) | Q(cliente_nombre__icontains=q))
    destinos = _valores_param(request, "destino", frozenset(DESTINOS), "Destino inexistente.")
    if destinos is not None:
        # Alguna programación a ese destino (``@>`` sobre el JSON).
        alguno = Q()
        for destino in destinos:
            alguno |= Q(programacion_conf__contains={"programaciones": [{"destino": destino}]})
        qs = qs.filter(alguno)
    etapas = _valores_param(request, "etapa", frozenset(ETAPAS), "Etapa inexistente.")
    if etapas is not None:
        qs = qs.annotate(etapa_sql=ETAPA_SQL).filter(etapa_sql__in=etapas)
    return qs


def contexto_por_surtir(pedidos):
    """``sucursal_nombre`` y ``programaciones`` de la página, como el onboarding
    (``cargar_catalogos``): almacén sugerido por destino y surtido por programación,
    una consulta cada uno para toda la página."""
    if not pedidos:
        return {}
    sucursales_por_empresa = defaultdict(set)
    for p in pedidos:
        sucursales_por_empresa[p.empresa_id].add(p.sucursal_id)
    almacenes = {}
    for empresa_id, sucursal_ids in sucursales_por_empresa.items():
        for sucursal_id, por_nombre in almacenes_por_destino_de_sucursales(empresa_id, sucursal_ids).items():
            almacenes[(empresa_id, sucursal_id)] = por_nombre
    surtido = surtido_por_destino_de_pedidos([p.pk for p in pedidos])
    return {
        p.pk: {
            "sucursal_nombre": p.sucursal_nombre,
            "programaciones": programaciones_de(p, almacenes[(p.empresa_id, p.sucursal_id)], surtido[p.pk]),
        }
        for p in pedidos
    }


def resumen_pestanas(qs):
    """Números de las pestañas de Pedidos WMS, sobre todo lo visible y en UNA consulta:
    ``por_surtir`` (= ``count`` de ``?por_surtir=1``) y ``surtidos``
    (= ``count`` de ``?estatus=todos&completo=1``)."""
    # El predicado va como anotación y no directo en ``filter=``: con subconsultas en
    # las anotaciones, ``aggregate`` envuelve la consulta y el ``OuterRef`` de
    # ``tiene_pendiente_surtible`` dentro del ``FILTER`` ya no encuentra la tabla.
    return qs.annotate(
        pedidas=_pedidas_pedido(), comprometidas=_comprometidas_por_talla(),
        es_por_surtir=Case(When(pedidos_por_surtir_q(), then=Value(True)), default=Value(False),
                           output_field=BooleanField()),
    ).order_by().aggregate(
        por_surtir=Count("id", filter=Q(es_por_surtir=True)),
        surtidos=Count("id", filter=COMPLETO_Q),
    )


def catalogos_filtros(qs):
    """Clientes y sucursales con al menos un pedido en el conjunto: 2 consultas.
    Mismo shape que ``pickings/filtros/`` (``[{id, nombre}]`` por nombre)."""
    qs = qs.order_by()
    clientes = qs.filter(cliente__isnull=False).values("cliente_id", "cliente__nombre").distinct()
    sucursales = qs.filter(sucursal__isnull=False).values("sucursal_id", "sucursal__nombre").distinct()

    def por_nombre(x):
        return ((x["nombre"] or "").upper(), x["id"])

    return {
        "clientes": sorted(({"id": c["cliente_id"], "nombre": c["cliente__nombre"]} for c in clientes), key=por_nombre),
        "sucursales": sorted(
            ({"id": s["sucursal_id"], "nombre": s["sucursal__nombre"]} for s in sucursales), key=por_nombre
        ),
    }


PARAMETROS_POBLACION_OPENAPI = (
    OpenApiParameter("estatus", OpenApiTypes.STR, description=(
        "Estatus separados por coma (por omisión `3,4`) o `todos`. Se ignora con `por_surtir=1`."
    )),
    OpenApiParameter("por_surtir", OpenApiTypes.BOOL, description=(
        "`1`/`true`: lo que ofrece el onboarding de picking (activo, estatus 3 o 4, programado, "
        "no retenido y con alguna talla surtible pendiente)."
    )),
    OpenApiParameter("completo", OpenApiTypes.BOOL, description=(
        "`1`/`true`: surtido completo (`pedidas > 0` y `comprometidas = 0`)."
    )),
    OpenApiParameter("pendientes", OpenApiTypes.BOOL, description="`1`/`true`: con `comprometidas > 0`."),
)
PARAMETROS_LISTADO_OPENAPI = (
    *PARAMETROS_POBLACION_OPENAPI,
    OpenApiParameter("q", OpenApiTypes.STR, description="Contiene, en el folio o en `cliente_nombre`."),
    OpenApiParameter("sucursal", OpenApiTypes.INT, description="Id de la sucursal del pedido."),
    OpenApiParameter("cliente", OpenApiTypes.INT, description="Id del cliente del pedido."),
    OpenApiParameter("destino", OpenApiTypes.STR, description=(
        "Uno o varios separados por coma: " + ", ".join(DESTINOS) + ". Pasa si alguna programación va a ese destino."
    )),
    OpenApiParameter("etapa", OpenApiTypes.STR, description="Una o varias separadas por coma: " + ", ".join(ETAPAS) + "."),
    OpenApiParameter("ordering", OpenApiTypes.STR, description=(
        "Campo de orden, `-` para descendente: " + ", ".join(CAMPOS_ORDEN) + ". Desempate por `id`; sin él, `-id`."
    )),
)


class _Pagina(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class SurtidoPedidoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    pagination_class = _Pagina
    # Sólo ``?ordering=`` con lista blanca (EC-988); los NULL (folio, fecha de
    # confirmación) al final. Sin él, el orden de ``get_queryset`` (``-id``).
    filter_backends = [OrdenamientoNullsAlFinal]
    ordering_fields = list(CAMPOS_ORDEN)

    def _por_surtir(self):
        return self.action == "list" and _bandera(self.request.query_params, "por_surtir")

    def _visibles(self):
        return documentos_wms_visibles(Pedido.objects.filter(activo=True), self.request.user)

    def get_queryset(self):
        qs = self._visibles()
        campos = _CAMPOS_FILA
        if self.action in ("list", "filtros"):
            qs = poblacion(qs, self.request.query_params)
            if self.action == "list":
                qs = filtrar(self.request, qs)
            if self._por_surtir():
                qs = qs.annotate(sucursal_nombre=F("sucursal__nombre"))
                campos = (*campos, "programacion_conf")
        else:
            qs = anotar_avance(qs, "pedido")
        return qs.only(*campos).order_by("-id")

    @extend_schema(
        summary="Avance de surtido por pedido",
        description=(
            "Paginado (`page`, `page_size` hasta 200). Con `por_surtir=1` cada fila trae además "
            "`sucursal_nombre` y `programaciones` (la forma del onboarding de picking)."
        ),
        parameters=[*PARAMETROS_LISTADO_OPENAPI],
        responses=OpenApiTypes.OBJECT,
    )
    def list(self, request, *args, **kwargs):
        pagina = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        filas = [_fila_pedido(o) for o in pagina]
        if self._por_surtir():
            extra = contexto_por_surtir(pagina)
            for fila in filas:
                fila.update(extra[fila["id"]])
        return self.get_paginated_response(filas)

    @extend_schema(
        summary="Catálogos de los filtros del avance de surtido",
        description=(
            "Clientes y sucursales con al menos un pedido en el conjunto (`estatus`, `por_surtir`, "
            "`completo`, `pendientes`), sin los filtros de columna: "
            "`{clientes: [{id, nombre}], sucursales: [{id, nombre}]}`."
        ),
        parameters=[*PARAMETROS_POBLACION_OPENAPI],
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"], url_path="filtros")
    def filtros(self, request):
        return Response(catalogos_filtros(self.get_queryset()))

    @extend_schema(
        summary="Números de las pestañas Por surtir y Surtidos",
        description=(
            "Sobre todo lo visible, sin parámetros (`q` y los filtros se ignoran): `generado_en`, "
            "`por_surtir` (= `count` de `?por_surtir=1`) y `surtidos` (= `count` de `?estatus=todos&completo=1`)."
        ),
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"], url_path="resumen")
    def resumen(self, request):
        return Response({"generado_en": timezone.now(), **resumen_pestanas(self._visibles())})

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
