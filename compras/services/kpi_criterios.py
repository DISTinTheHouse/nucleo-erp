"""Criterios de los KPIs de compras: UNA definición para los dos endpoints.

Los usan ``GET /compras/recepciones/kpis/`` (EC-434) y
``GET /terceros/proveedores/kpis/`` (EC-436). Antes cada uno tenía su copia de
los estatus "comprometidos", del cumplimiento de cantidad, del rechazo y de la
diferencia de precio, y una corrección en uno no llegaba al otro (#389).

Criterios:

- **Comprometida**: OC activa en estatus Autorizada, Parcialmente recibida o
  Recibida (no borrador ni cancelada).
- **Recibido neto**: lo capturado en recepciones de OC activas y no canceladas
  MENOS lo que Calidad rechazó; es el mismo cálculo que el estatus de la OC
  (``_cantidad_recibida_oc``), así el KPI y el estatus cuadran (#381).
- **Rechazo** (KPI de material rechazado y calidad del proveedor): renglón de
  Calidad con ``resultado = 'rechazo'`` en una recepción de OC; las recepciones
  de producción no cuentan (#382).
- **Diferencia de precio**: facturas Registradas y activas, línea contra su
  renglón de OC, solo si ambas están en la misma moneda (#378).
- Cantidades y montos nunca se suman entre unidades de medida ni entre
  monedas: se devuelven agrupados.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum

from compras.models import (
    CalidadInspeccionDetalle,
    OrdenCompra,
    OrdenCompraDetalle,
    Recepcion,
    RecepcionDetalle,
)
from finanzas.models import FacturaProveedor, FacturaProveedorDetalle

ESTATUS_OC_COMPROMETIDAS = (
    OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
    OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
    OrdenCompra.EstatusOrdenCompra.RECIBIDA,
)

RESULTADO_RECHAZO = "rechazo"
MONEY = DecimalField(max_digits=18, decimal_places=2)

_RUTA_OC_DETALLE = "orden_compra__"
_RUTA_RECEPCION_DETALLE = "orden_compra_detalle__orden_compra__"
_RUTA_CALIDAD = "recepcion_detalle__orden_compra_detalle__orden_compra__"
_RUTA_FACTURA_DETALLE = "oc_detalle__orden_compra__"


@dataclass(frozen=True)
class AlcanceCompras:
    """Qué OCs entran: empresa (``None`` = todas, solo superusuario) y filtros
    opcionales. Todo se aplica sobre la OC, así cualquier KPI usa el mismo
    universo."""

    empresa: object = None
    sucursal_id: int | None = None
    proveedor_id: int | None = None
    fecha_desde: date | None = None
    fecha_hasta: date | None = None
    solo_proveedores_activos: bool = False

    def oc(self, ruta: str = "") -> Q:
        """``Q`` sobre ``OrdenCompra``; ``ruta`` es el camino hasta ella
        (p. ej. ``"orden_compra__"``)."""
        q = Q(**{f"{ruta}activo": True})
        if self.empresa is not None:
            q &= Q(**{f"{ruta}empresa": self.empresa})
        if self.sucursal_id is not None:
            q &= Q(**{f"{ruta}sucursal_id": self.sucursal_id})
        if self.proveedor_id is not None:
            q &= Q(**{f"{ruta}proveedor_id": self.proveedor_id})
        if self.solo_proveedores_activos:
            q &= Q(**{f"{ruta}proveedor__activo": True})
        if self.fecha_desde is not None:
            q &= Q(**{f"{ruta}fecha_oc__gte": self.fecha_desde})
        if self.fecha_hasta is not None:
            q &= Q(**{f"{ruta}fecha_oc__lte": self.fecha_hasta})
        return q

    def como_dict(self) -> dict:
        return {
            "sucursal": self.sucursal_id,
            "proveedor": self.proveedor_id,
            "fecha_desde": self.fecha_desde,
            "fecha_hasta": self.fecha_hasta,
        }


# --- querysets base ---------------------------------------------------------

def oc_comprometidas(alcance: AlcanceCompras):
    return OrdenCompra.objects.filter(alcance.oc(), estatus__in=ESTATUS_OC_COMPROMETIDAS)


def oc_detalles_comprometidos(alcance: AlcanceCompras):
    return OrdenCompraDetalle.objects.filter(
        alcance.oc(_RUTA_OC_DETALLE), orden_compra__estatus__in=ESTATUS_OC_COMPROMETIDAS
    )


def recibido_bruto_oc(alcance: AlcanceCompras):
    """Renglones de recepción de OC (activas, no canceladas) sobre OCs comprometidas."""
    return RecepcionDetalle.objects.filter(
        alcance.oc(_RUTA_RECEPCION_DETALLE),
        orden_compra_detalle__orden_compra__estatus__in=ESTATUS_OC_COMPROMETIDAS,
        recepcion__activo=True,
        recepcion__tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA,
    ).exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)


def inspecciones_oc(alcance: AlcanceCompras):
    """Renglones de Calidad de recepciones de OC activas y no canceladas."""
    return CalidadInspeccionDetalle.objects.filter(
        alcance.oc(_RUTA_CALIDAD),
        recepcion_detalle__recepcion__activo=True,
        recepcion_detalle__recepcion__tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA,
    ).exclude(recepcion_detalle__recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)


def rechazos_oc(alcance: AlcanceCompras):
    """Rechazos de Calidad (``resultado = 'rechazo'`` con cantidad rechazada)."""
    return inspecciones_oc(alcance).filter(resultado=RESULTADO_RECHAZO, cantidad_rechazada__gt=0)


def rechazado_para_neto(alcance: AlcanceCompras):
    """Lo que el estatus de la OC resta de lo recibido: cualquier cantidad
    rechazada por Calidad, sobre OCs comprometidas."""
    return inspecciones_oc(alcance).filter(
        recepcion_detalle__orden_compra_detalle__orden_compra__estatus__in=ESTATUS_OC_COMPROMETIDAS,
        cantidad_rechazada__gt=0,
    )


def lineas_factura_proveedor(alcance: AlcanceCompras):
    """Líneas de factura Registrada y activa contra un renglón de OC comprometido."""
    return FacturaProveedorDetalle.objects.filter(
        alcance.oc(_RUTA_FACTURA_DETALLE),
        factura_proveedor__activo=True,
        factura_proveedor__estatus=FacturaProveedor.FacturaProveedorStatus.REGISTRADA,
        oc_detalle__orden_compra__estatus__in=ESTATUS_OC_COMPROMETIDAS,
    )


def con_importes_precio(qs):
    """Agrega ``facturado_linea`` y ``pactado_linea`` (precio × cantidad)."""
    return qs.annotate(
        facturado_linea=ExpressionWrapper(F("precio_unitario") * F("cantidad"), output_field=MONEY),
        pactado_linea=ExpressionWrapper(F("oc_detalle__precio") * F("cantidad"), output_field=MONEY),
    )


def misma_moneda():
    """Línea de factura cuya factura está en la moneda de su OC."""
    return Q(factura_proveedor__moneda_id=F("oc_detalle__orden_compra__moneda_id"))


# --- cumplimiento de cantidad ----------------------------------------------

def cumplimiento_por_unidad(alcance: AlcanceCompras, por_proveedor: bool = False):
    """Ordenado vs. recibido NETO, una fila por unidad de medida (y por
    proveedor de la OC si ``por_proveedor``).

    ``unidad_id`` ``None`` = productos sin unidad capturada. Devuelve
    ``{(proveedor_id, unidad_id): {...}}`` (``proveedor_id`` ``None`` si no se
    agrupa por proveedor).
    """
    unidad = "producto__unidad_medida_id"
    clave = "producto__unidad_medida__clave"

    def campos(prefijo_detalle, prefijo_oc):
        # ``prefijo_detalle``: camino al renglón de OC; ``prefijo_oc``: a la OC.
        base = [f"{prefijo_detalle}{unidad}", f"{prefijo_detalle}{clave}"]
        if por_proveedor:
            base += [f"{prefijo_oc}proveedor_id", f"{prefijo_oc}proveedor__nombre"]
        return base

    por_unidad: dict = {}

    def fila(r, campos_):
        uid, cve = r[campos_[0]], r[campos_[1]]
        pid, nombre = (r[campos_[2]], r[campos_[3]]) if por_proveedor else (None, None)
        return por_unidad.setdefault((pid, uid), {
            "proveedor_id": pid, "proveedor_nombre": nombre, "unidad_id": uid, "unidad": cve,
            "cantidad_ordenada": Decimal("0"), "cantidad_recibida": Decimal("0"),
        })

    c = campos("", "orden_compra__")
    for r in oc_detalles_comprometidos(alcance).values(*c).annotate(t=Sum("cantidad")).order_by():
        fila(r, c)["cantidad_ordenada"] += r["t"] or 0

    c = campos("orden_compra_detalle__", "orden_compra_detalle__orden_compra__")
    for r in recibido_bruto_oc(alcance).values(*c).annotate(t=Sum("cantidad_recibida")).order_by():
        fila(r, c)["cantidad_recibida"] += r["t"] or 0

    c = campos("recepcion_detalle__orden_compra_detalle__", "recepcion_detalle__orden_compra_detalle__orden_compra__")
    for r in rechazado_para_neto(alcance).values(*c).annotate(t=Sum("cantidad_rechazada")).order_by():
        fila(r, c)["cantidad_recibida"] -= r["t"] or 0

    return por_unidad


def porcentaje(parte, total):
    return round(float(parte) / float(total) * 100, 1) if total else None


def cumplimiento_pendiente_por_oc(alcance: AlcanceCompras):
    """OCs con algo pendiente (recibido neto < ordenado), por OC y unidad de
    medida, peor cobertura primero. Una OC que recibió de más en una unidad no
    tapa lo que falta en otra."""
    unidad = "producto__unidad_medida_id"
    clave = "producto__unidad_medida__clave"
    filas: dict = {}

    def fila(oc_id, folio, uid, cve):
        return filas.setdefault((oc_id, uid), {
            "oc_id": oc_id, "folio": folio, "unidad_id": uid, "unidad": cve,
            "cantidad_ordenada": Decimal("0"), "cantidad_recibida": Decimal("0"),
        })

    for r in (
        oc_detalles_comprometidos(alcance)
        .values("orden_compra_id", "orden_compra__folio", unidad, clave)
        .annotate(t=Sum("cantidad")).order_by()
    ):
        fila(r["orden_compra_id"], r["orden_compra__folio"], r[unidad], r[clave])["cantidad_ordenada"] += r["t"] or 0

    oc_rec = "orden_compra_detalle__orden_compra_id"
    for r in (
        recibido_bruto_oc(alcance)
        .values(oc_rec, "orden_compra_detalle__orden_compra__folio", f"orden_compra_detalle__{unidad}")
        .annotate(t=Sum("cantidad_recibida")).order_by()
    ):
        f = filas.get((r[oc_rec], r[f"orden_compra_detalle__{unidad}"]))
        if f:
            f["cantidad_recibida"] += r["t"] or 0

    oc_rech = "recepcion_detalle__orden_compra_detalle__orden_compra_id"
    for r in (
        rechazado_para_neto(alcance)
        .values(oc_rech, f"recepcion_detalle__orden_compra_detalle__{unidad}")
        .annotate(t=Sum("cantidad_rechazada")).order_by()
    ):
        f = filas.get((r[oc_rech], r[f"recepcion_detalle__orden_compra_detalle__{unidad}"]))
        if f:
            f["cantidad_recibida"] -= r["t"] or 0

    pendientes = []
    for f in filas.values():
        if f["cantidad_recibida"] >= f["cantidad_ordenada"]:
            continue
        f["pct"] = porcentaje(f["cantidad_recibida"], f["cantidad_ordenada"]) or 0.0
        pendientes.append(f)
    pendientes.sort(key=lambda d: (d["pct"], d["oc_id"], d["unidad_id"] or 0))
    return pendientes
