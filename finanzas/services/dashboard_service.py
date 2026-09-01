from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum, Q, Count
from django.utils import timezone

from finanzas.models import (
    AlertaMora,
    Banco,
    Cobro,
    CuentaBancaria,
    CuentaPorCobrar,
    CuentaPorPagar,
    Pago,
)


def _quantize(v):
    return (Decimal("0") if v is None else Decimal(str(v))).quantize(Decimal("0.01"))


class DashboardFinancieroService:
    @staticmethod
    def obtener_resumen(*, empresa, fecha_inicio=None, fecha_fin=None, moneda_id=None):
        hoy = timezone.localdate()
        if fecha_inicio is None:
            fecha_inicio = hoy.replace(day=1)
        if fecha_fin is None:
            fecha_fin = hoy
        dentro_7 = hoy + timedelta(days=7)

        cxc_qs = CuentaPorCobrar.objects.exclude(
            estatus__in=[CuentaPorCobrar.EstatusCxC.CANCELADA]
        )
        if empresa:
            cxc_qs = cxc_qs.filter(
                Q(empresa=empresa) | Q(factura__empresa=empresa)
            ).distinct()
        if moneda_id:
            cxc_qs = cxc_qs.filter(factura__moneda_id=moneda_id)

        cxc_vencida = cxc_qs.filter(
            saldo__gt=0,
            fecha_vencimiento__isnull=False,
            fecha_vencimiento__lt=hoy,
        )
        cxc_x_vencer_7 = cxc_qs.filter(
            saldo__gt=0,
            fecha_vencimiento__isnull=False,
            fecha_vencimiento__gte=hoy,
            fecha_vencimiento__lte=dentro_7,
        )
        total_cxc = cxc_qs.count()
        saldo_total_cxc = _quantize(cxc_qs.aggregate(s=Sum("saldo"))["s"] or 0)
        cantidad_cxc_vencida = cxc_vencida.count()
        monto_cxc_vencida = _quantize(cxc_vencida.aggregate(s=Sum("saldo"))["s"] or 0)
        cantidad_cxc_por_vencer = cxc_x_vencer_7.count()
        monto_cxc_por_vencer = _quantize(cxc_x_vencer_7.aggregate(s=Sum("saldo"))["s"] or 0)

        cxp_qs = CuentaPorPagar.objects.exclude(
            estatus__in=[CuentaPorPagar.EstatusCxP.CANCELADA]
        )
        if empresa:
            cxp_qs = cxp_qs.filter(empresa=empresa)
        if moneda_id:
            cxp_qs = cxp_qs.filter(factura_proveedor__moneda_id=moneda_id)

        cxp_vencida = cxp_qs.filter(
            saldo__gt=0,
            fecha_vencimiento__isnull=False,
            fecha_vencimiento__lt=hoy,
        )
        total_cxp = cxp_qs.count()
        saldo_total_cxp = _quantize(cxp_qs.aggregate(s=Sum("saldo"))["s"] or 0)
        cantidad_cxp_vencida = cxp_vencida.count()
        monto_cxp_vencida = _quantize(cxp_vencida.aggregate(s=Sum("saldo"))["s"] or 0)

        cobros_periodo = Cobro.objects.filter(
            fecha_cobro__gte=fecha_inicio,
            fecha_cobro__lte=fecha_fin,
            estatus=Cobro.Estatus.APLICADO,
        )
        if empresa:
            cobros_periodo = cobros_periodo.filter(empresa=empresa)
        if moneda_id:
            cobros_periodo = cobros_periodo.filter(cuenta_bancaria__moneda_id=moneda_id)
        total_cobros = _quantize(
            cobros_periodo.aggregate(s=Sum("total_cobrado"))["s"] or 0
        )
        cantidad_cobros = cobros_periodo.count()

        pagos_periodo = Pago.objects.filter(
            fecha_pago__gte=fecha_inicio,
            fecha_pago__lte=fecha_fin,
            estatus=Pago.Estatus.APLICADO,
        )
        if empresa:
            pagos_periodo = pagos_periodo.filter(empresa=empresa)
        if moneda_id:
            pagos_periodo = pagos_periodo.filter(cuenta_bancaria__moneda_id=moneda_id)
        total_pagos = _quantize(
            pagos_periodo.aggregate(s=Sum("total_pagado"))["s"] or 0
        )
        cantidad_pagos = pagos_periodo.count()

        cobros_por_dia = defaultdict(lambda: Decimal("0.00"))
        for c in cobros_periodo.values("fecha_cobro").annotate(s=Sum("total_cobrado")):
            cobros_por_dia[str(c["fecha_cobro"])] += _quantize(c["s"])
        pagos_por_dia = defaultdict(lambda: Decimal("0.00"))
        for p in pagos_periodo.values("fecha_pago").annotate(s=Sum("total_pagado")):
            pagos_por_dia[str(p["fecha_pago"])] += _quantize(p["s"])

        cuentas_qs = CuentaBancaria.objects.filter(activo=True)
        if empresa:
            cuentas_qs = cuentas_qs.filter(empresa=empresa)
        if moneda_id:
            cuentas_qs = cuentas_qs.filter(moneda_id=moneda_id)
        cuentas = list(
            cuentas_qs.select_related("banco", "moneda")
            .order_by("banco__nombre", "alias")
            .all()
        )
        saldos_por_moneda = defaultdict(lambda: Decimal("0.00"))
        lista_cuentas = []
        for cta in cuentas:
            cod = getattr(cta.moneda, "codigo_iso", "XXX") or "XXX"
            saldos_por_moneda[cod] += _quantize(cta.saldo_actual)
            lista_cuentas.append(
                {
                    "id": cta.pk,
                    "alias": cta.alias,
                    "numero_cuenta": cta.numero_cuenta,
                    "clabe": cta.clabe,
                    "banco": getattr(cta.banco, "nombre", None),
                    "moneda": cod,
                    "saldo_actual": str(_quantize(cta.saldo_actual)),
                }
            )
        saldos_por_moneda_out = {k: str(v.quantize(Decimal("0.01"))) for k, v in saldos_por_moneda.items()}

        top_clientes = []
        tcs = (
            cxc_qs.filter(saldo__gt=0)
            .values("cliente_id", "cliente__nombre")
            .annotate(saldo=Sum("saldo"), cantidad=Count("id"))
            .order_by("-saldo")[:5]
        )
        for r in tcs:
            top_clientes.append(
                {
                    "cliente_id": r["cliente_id"],
                    "cliente_nombre": r["cliente__nombre"],
                    "saldo": str(_quantize(r["saldo"])),
                    "cantidad": r["cantidad"],
                }
            )

        top_proveedores = []
        tps = (
            cxp_qs.filter(saldo__gt=0)
            .values("proveedor_id", "proveedor__nombre")
            .annotate(saldo=Sum("saldo"), cantidad=Count("id"))
            .order_by("-saldo")[:5]
        )
        for r in tps:
            top_proveedores.append(
                {
                    "proveedor_id": r["proveedor_id"],
                    "proveedor_nombre": r["proveedor__nombre"],
                    "saldo": str(_quantize(r["saldo"])),
                    "cantidad": r["cantidad"],
                }
            )

        alertas_qs = AlertaMora.objects.all()
        if empresa:
            alertas_qs = alertas_qs.filter(empresa=empresa)
        alertas_stats = {
            "total": alertas_qs.count(),
            "leve": alertas_qs.filter(nivel=AlertaMora.Nivel.LEVE).count(),
            "moderada": alertas_qs.filter(nivel=AlertaMora.Nivel.MODERADA).count(),
            "grave": alertas_qs.filter(nivel=AlertaMora.Nivel.GRAVE).count(),
            "critica": alertas_qs.filter(nivel=AlertaMora.Nivel.CRITICA).count(),
        }

        return {
            "periodo": {
                "fecha_inicio": fecha_inicio,
                "fecha_fin": fecha_fin,
            },
            "cuentas_por_cobrar": {
                "total_documentos": total_cxc,
                "saldo_total": str(saldo_total_cxc),
                "vencida": {
                    "cantidad": cantidad_cxc_vencida,
                    "monto": str(monto_cxc_vencida),
                },
                "por_vencer_7_dias": {
                    "cantidad": cantidad_cxc_por_vencer,
                    "monto": str(monto_cxc_por_vencer),
                },
                "top_clientes": top_clientes,
            },
            "cuentas_por_pagar": {
                "total_documentos": total_cxp,
                "saldo_total": str(saldo_total_cxp),
                "vencida": {
                    "cantidad": cantidad_cxp_vencida,
                    "monto": str(monto_cxp_vencida),
                },
                "top_proveedores": top_proveedores,
            },
            "cobros_periodo": {
                "cantidad": cantidad_cobros,
                "monto_total": str(total_cobros),
                "por_dia": {k: str(v) for k, v in cobros_por_dia.items()},
            },
            "pagos_periodo": {
                "cantidad": cantidad_pagos,
                "monto_total": str(total_pagos),
                "por_dia": {k: str(v) for k, v in pagos_por_dia.items()},
            },
            "bancos": {
                "saldos_por_moneda": saldos_por_moneda_out,
                "cuentas": lista_cuentas,
            },
            "alertas_mora": alertas_stats,
        }
