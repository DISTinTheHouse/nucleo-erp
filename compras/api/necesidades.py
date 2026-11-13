"""Necesidades de compra (faltantes de pre-órdenes de producción).

- ``GET /compras/necesidades/``: una fila por necesidad (filtros ``?estatus``,
  ``?pre_orden``, ``?producto``).
- ``GET /compras/necesidades/agrupadas/``: las pendientes, una fila por material
  (y sucursal) con lo que compras necesita para decidir.
- ``POST /compras/ordenes/desde-necesidades/`` (``crear_oc_desde_necesidades``):
  una OC consolidada en Borrador.

Alcance: siempre ``user.empresa`` (superusuario incluido), como el listado de OC.
"""
import math
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, serializers
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from compras.models import NecesidadCompra, OrdenCompra, OrdenCompraDetalle
from nucleo.kpis import entero_param

N = NecesidadCompra.Estatus
CERO = Decimal("0")


def _f(x):
    return float(x) if x is not None else None


class NecesidadCompraSerializer(serializers.ModelSerializer):
    material = serializers.IntegerField(source="producto_id", read_only=True)
    material_nombre = serializers.CharField(source="producto.nombre", read_only=True)
    material_codigo = serializers.SerializerMethodField()
    unidad = serializers.CharField(source="unidad.clave", read_only=True, allow_null=True, default=None)
    pedido_folio = serializers.CharField(source="pedido.folio", read_only=True, allow_null=True, default=None)
    origen = serializers.CharField(source="pre_orden.origen", read_only=True)
    referencia = serializers.SerializerMethodField()
    orden_compra_folio = serializers.CharField(source="orden_compra.folio", read_only=True, allow_null=True, default=None)
    estatus_display = serializers.CharField(source="get_estatus_display", read_only=True)
    cantidad = serializers.FloatField(read_only=True)
    cantidad_recibida = serializers.FloatField(read_only=True)
    sobrante = serializers.FloatField(read_only=True)

    class Meta:
        model = NecesidadCompra
        fields = [
            "id", "material", "material_nombre", "material_codigo", "cantidad", "unidad", "pre_orden",
            "pedido", "pedido_folio", "origen", "referencia", "fecha_requerida", "estatus", "estatus_display", "orden_compra",
            "orden_compra_folio", "cantidad_recibida", "sobrante", "sucursal", "created_at",
        ]

    def get_material_codigo(self, obj):
        return obj.producto.cod_proscai or obj.producto.codigo or ""

    def get_referencia(self, obj):
        return _referencia(obj)


def _referencia(n):
    """``P-…`` (folio del pedido) o ``Resurtido · SKU`` para mostrar."""
    from produccion.services.pre_orden_service import referencia
    r = referencia(n.pre_orden)
    return r[len("pedido "):] if r.startswith("pedido ") else r


def _ultimas_compras(empresa_id, productos):
    """``{producto_id: (proveedor_id, proveedor_nombre, precio, fecha_oc)}`` del
    último renglón comprado (OC autorizada o recibida). 1 consulta (DISTINCT ON)."""
    if not productos:
        return {}
    E = OrdenCompra.EstatusOrdenCompra
    filas = (
        OrdenCompraDetalle.objects.filter(
            producto_id__in=list(productos), orden_compra__empresa_id=empresa_id, orden_compra__activo=True,
            orden_compra__estatus__in=[E.AUTORIZADA, E.PARCIALMENTE_RECIBIDA, E.RECIBIDA],
        )
        .order_by("producto_id", "-orden_compra__fecha_oc", "-id").distinct("producto_id")
        .values_list("producto_id", "orden_compra__proveedor_id", "orden_compra__proveedor__nombre", "precio", "orden_compra__fecha_oc")
    )
    return {f[0]: f[1:] for f in filas}


class NecesidadCompraViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, GenericViewSet):
    serializer_class = NecesidadCompraSerializer

    def get_queryset(self):
        empresa = getattr(self.request.user, "empresa", None)
        if empresa is None:
            return NecesidadCompra.objects.none()
        qs = NecesidadCompra.objects.filter(empresa=empresa).select_related(
            "producto", "unidad", "pedido", "orden_compra", "pre_orden__pedido", "pre_orden__producto"
        ).order_by("fecha_requerida", "id")
        p = self.request.query_params
        if p.get("estatus"):
            qs = qs.filter(estatus__in=[e for e in p["estatus"].split(",") if e])
        for campo in ("pre_orden", "producto", "orden_compra"):
            v = entero_param(self.request, campo)
            if v is not None:
                qs = qs.filter(**{f"{campo}_id": v})
        return qs

    @action(detail=False, methods=["get"], url_path="resumen")
    def resumen(self, request):
        """Badge del menú: ``{pendientes}`` (necesidades pendientes). 1 consulta."""
        return Response({"pendientes": self.get_queryset().filter(estatus=N.PENDIENTE).count()})

    @action(detail=False, methods=["get"], url_path="agrupadas")
    def agrupadas(self, request):
        """Pendientes por material y sucursal: total, pedidos, fecha más urgente,
        existencia, en tránsito, proveedor habitual y último costo (de la última
        compra del material) y las necesidades que agrupa. 4 consultas + 2 por
        sucursal distinta."""
        from produccion.services.pre_orden_service import en_transito, existencias
        empresa = getattr(request.user, "empresa", None)
        if empresa is None:
            return Response([])
        necs = list(self.get_queryset().filter(estatus=N.PENDIENTE))
        grupos = defaultdict(list)
        for n in necs:
            grupos[(n.sucursal_id, n.producto_id)].append(n)
        productos_por_suc = defaultdict(set)
        for suc, prod in grupos:
            productos_por_suc[suc].add(prod)
        ex, tr = {}, {}
        for suc, prods in productos_por_suc.items():
            ex[suc] = existencias(empresa.pk, prods, suc)
            tr[suc] = en_transito(empresa.pk, prods, suc)
        ultimas = _ultimas_compras(empresa.pk, {p for _, p in grupos})
        filas = []
        for (suc, prod), lista in grupos.items():
            n0 = lista[0]
            fechas = [n.fecha_requerida for n in lista if n.fecha_requerida]
            u = ultimas.get(prod)
            filas.append({
                "material": prod,
                "material_nombre": n0.producto.nombre,
                "material_codigo": n0.producto.cod_proscai or n0.producto.codigo or "",
                "unidad": n0.unidad.clave if n0.unidad_id else None,
                "sucursal": suc,
                "total_requerido": _f(sum((n.cantidad for n in lista), CERO)),
                "pedidos": len({n.pedido_id for n in lista}),
                "fecha_mas_urgente": min(fechas) if fechas else None,
                "existencia": _f(ex[suc].get(prod, CERO)),
                "en_transito": _f(tr[suc].get(prod, CERO)),
                "proveedor_habitual": {"id": u[0], "nombre": u[1]} if u and u[0] else None,
                "ultimo_costo": _f(u[2]) if u else None,
                "necesidades": [
                    {"id": n.pk, "cantidad": _f(n.cantidad), "pre_orden": n.pre_orden_id, "pedido": n.pedido_id,
                     "pedido_folio": n.pedido.folio if n.pedido_id else None, "origen": n.pre_orden.origen,
                     "referencia": _referencia(n), "fecha_requerida": n.fecha_requerida}
                    for n in lista
                ],
            })
        filas.sort(key=lambda f: (f["fecha_mas_urgente"] is None, f["fecha_mas_urgente"] or timezone.localdate(), f["material_nombre"] or ""))
        return Response(filas)


def redondear_compra(cantidad, producto):
    """Piezas a pedir: entero hacia arriba y, si el material lo define, al
    mínimo y al múltiplo de compra (``minimo_compra``/``multiplo_compra``)."""
    n = max(int(math.ceil(cantidad)), 1)
    minimo = getattr(producto, "minimo_compra", None)
    multiplo = getattr(producto, "multiplo_compra", None)
    if minimo:
        n = max(n, int(math.ceil(minimo)))
    if multiplo and multiplo > 0:
        m = Decimal(str(multiplo))
        n = int(math.ceil(Decimal(n) / m) * m)
    return n


@transaction.atomic
def crear_oc_desde_necesidades(user, proveedor_id, necesidad_ids):
    """Una OC en Borrador con las necesidades pendientes, un renglón por
    material (redondeado con ``redondear_compra``). Las necesidades pasan a
    ``en_oc``; cada pre-orden registra ``oc_creada``. Devuelve la OC."""
    from nucleo.models import Moneda
    from terceros.models import Proveedor
    from produccion.services.pre_orden_service import eventos_oc
    from produccion.models import PreOrdenEvento
    empresa = getattr(user, "empresa", None)
    if empresa is None:
        raise ValidationError({"empresa": "El usuario no tiene una empresa asignada."})
    try:
        ids = {int(x) for x in (necesidad_ids or [])}
    except (TypeError, ValueError):
        raise ValidationError({"necesidades": "Lista de ids de necesidades."})
    if not ids:
        raise ValidationError({"necesidades": "Selecciona al menos una necesidad."})
    proveedor = Proveedor.objects.filter(pk=proveedor_id, empresa=empresa, activo=True).first() if proveedor_id else None
    if proveedor is None:
        raise ValidationError({"proveedor": "Proveedor no válido."})
    necs = list(
        NecesidadCompra.objects.select_for_update().filter(pk__in=ids, empresa=empresa).select_related("producto")
    )
    if len(necs) != len(ids):
        raise ValidationError({"necesidades": "Alguna necesidad no existe."})
    if any(n.estatus != N.PENDIENTE for n in necs):
        raise ValidationError({"necesidades": "Solo se consolidan necesidades pendientes."})
    sucursales = {n.sucursal_id for n in necs}
    if len(sucursales) > 1:
        raise ValidationError({"necesidades": "Las necesidades son de sucursales distintas; crea una OC por sucursal."})
    sucursal_id = sucursales.pop()
    moneda = (
        getattr(empresa, "moneda_base", None)
        or Moneda.objects.filter(empresa__isnull=True, codigo_iso="MXN", activo=True).first()
        or Moneda.objects.filter(empresa__isnull=True, activo=True).order_by("codigo_iso").first()
    )
    por_material = defaultdict(list)
    for n in necs:
        por_material[n.producto_id].append(n)
    piezas = {p: redondear_compra(sum((n.cantidad for n in lista), CERO), lista[0].producto) for p, lista in por_material.items()}
    oc = OrdenCompra.objects.create(
        empresa=empresa, sucursal_id=sucursal_id, proveedor=proveedor, moneda=moneda, usuario=user,
        tipo_compra=OrdenCompra.TipoCompra.MATERIA_PRIMA, estatus=OrdenCompra.EstatusOrdenCompra.BORRADOR,
        fecha_oc=timezone.localdate(), total_piezas=sum(piezas.values()),
        observaciones="Consolidada desde necesidades de compra de pre-órdenes de producción.",
    )
    oc.pedidos.set({n.pedido_id for n in necs if n.pedido_id})
    for prod, lista in por_material.items():
        det = OrdenCompraDetalle.objects.create(
            orden_compra=oc, producto_id=prod, descripcion=(lista[0].producto.nombre or "")[:200],
            cantidad=piezas[prod], piezas=piezas[prod], sucursal_id=sucursal_id,
        )
        NecesidadCompra.objects.filter(pk__in=[n.pk for n in lista]).update(
            estatus=N.EN_OC, orden_compra=oc, orden_compra_detalle=det, updated_at=timezone.now(),
        )
    eventos_oc(oc.pk, PreOrdenEvento.Evento.OC_CREADA, user)
    return oc
