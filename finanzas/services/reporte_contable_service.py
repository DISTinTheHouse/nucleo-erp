"""Reportes contables sobre las pólizas: libro diario, auxiliar de cuenta y balanza.

Cuentan las pólizas Contabilizadas y las Canceladas (una Cancelada fue contabilizada y su
contrapóliza la neutraliza). Los Borradores no cuentan. Todo se calcula en la base con
agregados: el número de consultas no depende del número de pólizas. Sin conversión de moneda.
"""

from decimal import Decimal

from django.db.models import F, Q, Sum, Window

from finanzas.models import CuentaContable, Poliza, PolizaDetalle

CERO = Decimal("0.00")
CENTAVO = Decimal("0.01")
DEUDORAS = {CuentaContable.CuentaTipo.ACTIVO, CuentaContable.CuentaTipo.GASTO, CuentaContable.CuentaTipo.COSTO}
LIMITE_MAXIMO = 1000
LIMITE_OMISION = 200


def _renglones(empresa_id):
    """Renglones de pólizas que cuentan para los reportes."""
    return PolizaDetalle.objects.filter(
        poliza__empresa_id=empresa_id,
        poliza__estatus__in=[Poliza.PolizaStatus.CONTABILIZADA, Poliza.PolizaStatus.CANCELADA],
    )


def _dos(valor):
    return str(Decimal(valor or 0).quantize(CENTAVO))


def _naturaleza(tipo):
    return "Deudora" if tipo in DEUDORAS else "Acreedora"


def _saldo(tipo, cargos, abonos):
    """Saldo en la naturaleza de la cuenta: deudora cargos - abonos, acreedora abonos - cargos."""
    cargos, abonos = Decimal(cargos or 0), Decimal(abonos or 0)
    return cargos - abonos if tipo in DEUDORAS else abonos - cargos


def _pagina(limit, offset):
    limit = min(max(limit, 1), LIMITE_MAXIMO)
    return limit, max(offset, 0)


class ReporteContableService:
    @staticmethod
    def libro_diario(empresa_id, fecha_inicio, fecha_fin, *, sucursal=None, tipo=None,
                     limit=LIMITE_OMISION, offset=0):
        limit, offset = _pagina(limit, offset)
        qs = _renglones(empresa_id).filter(poliza__fecha__gte=fecha_inicio, poliza__fecha__lte=fecha_fin)
        if sucursal:
            qs = qs.filter(poliza__sucursal_id=sucursal)
        if tipo:
            qs = qs.filter(poliza__tipo=tipo)
        totales = qs.aggregate(cargos=Sum("cargo"), abonos=Sum("abono"))
        filas = (
            qs.order_by("poliza__fecha", "poliza_id", "orden", "id")
            .values(
                "poliza_id", "poliza__fecha", "poliza__folio", "poliza__tipo", "poliza__concepto",
                "poliza__estatus", "poliza__origen_tipo", "poliza__origen_id",
                "poliza__poliza_reversa_de_id", "cuenta_contable_id", "cuenta_contable__codigo",
                "cuenta_contable__nombre", "cargo", "abono", "referencia",
            )[offset:offset + limit]
        )
        return {
            "total_cargos": _dos(totales["cargos"]),
            "total_abonos": _dos(totales["abonos"]),
            "limit": limit,
            "offset": offset,
            "renglones": [
                {
                    "poliza_id": f["poliza_id"], "fecha": f["poliza__fecha"], "folio": f["poliza__folio"],
                    "tipo": f["poliza__tipo"], "concepto": f["poliza__concepto"],
                    "estatus": f["poliza__estatus"], "origen_tipo": f["poliza__origen_tipo"],
                    "origen_id": f["poliza__origen_id"], "contrapoliza_de": f["poliza__poliza_reversa_de_id"],
                    "cuenta_id": f["cuenta_contable_id"], "cuenta_codigo": f["cuenta_contable__codigo"],
                    "cuenta_nombre": f["cuenta_contable__nombre"], "cargo": _dos(f["cargo"]),
                    "abono": _dos(f["abono"]), "referencia": f["referencia"],
                }
                for f in filas
            ],
        }

    @staticmethod
    def auxiliar(empresa_id, cuenta, fecha_inicio, fecha_fin, *, limit=LIMITE_OMISION, offset=0):
        """Movimientos de una cuenta con saldo corrido. ``cuenta`` es un ``CuentaContable`` de la empresa."""
        limit, offset = _pagina(limit, offset)
        base = _renglones(empresa_id).filter(cuenta_contable=cuenta)
        previo = base.filter(poliza__fecha__lt=fecha_inicio).aggregate(c=Sum("cargo"), a=Sum("abono"))
        periodo = base.filter(poliza__fecha__gte=fecha_inicio, poliza__fecha__lte=fecha_fin)
        totales = periodo.aggregate(c=Sum("cargo"), a=Sum("abono"))
        saldo_inicial = _saldo(cuenta.tipo, previo["c"], previo["a"])
        signo = 1 if cuenta.tipo in DEUDORAS else -1
        filas = (
            periodo.order_by("poliza__fecha", "poliza_id", "orden", "id")
            .annotate(
                acumulado=Window(
                    Sum(F("cargo") - F("abono")),
                    order_by=[F("poliza__fecha").asc(), F("poliza_id").asc(), F("orden").asc(), F("id").asc()],
                )
            )
            .values("poliza_id", "poliza__fecha", "poliza__folio", "poliza__concepto", "referencia",
                    "cargo", "abono", "acumulado")[offset:offset + limit]
        )
        return {
            "cuenta": {"id": cuenta.pk, "codigo": cuenta.codigo, "nombre": cuenta.nombre,
                       "tipo": cuenta.tipo, "naturaleza": _naturaleza(cuenta.tipo)},
            "saldo_inicial": _dos(saldo_inicial),
            "total_cargos": _dos(totales["c"]),
            "total_abonos": _dos(totales["a"]),
            "saldo_final": _dos(saldo_inicial + _saldo(cuenta.tipo, totales["c"], totales["a"])),
            "limit": limit,
            "offset": offset,
            "movimientos": [
                {
                    "poliza_id": f["poliza_id"], "fecha": f["poliza__fecha"], "folio": f["poliza__folio"],
                    "concepto": f["poliza__concepto"], "referencia": f["referencia"],
                    "cargo": _dos(f["cargo"]), "abono": _dos(f["abono"]),
                    "saldo": _dos(saldo_inicial + signo * Decimal(f["acumulado"] or 0)),
                }
                for f in filas
            ],
        }

    @staticmethod
    def balanza(empresa_id, fecha_inicio, fecha_fin):
        """Por cuenta: saldo inicial, cargos y abonos del periodo y saldo final. Una sola consulta."""
        antes = Q(poliza__fecha__lt=fecha_inicio)
        durante = Q(poliza__fecha__gte=fecha_inicio, poliza__fecha__lte=fecha_fin)
        filas = (
            _renglones(empresa_id)
            .filter(poliza__fecha__lte=fecha_fin)
            .values("cuenta_contable_id", "cuenta_contable__codigo", "cuenta_contable__nombre",
                    "cuenta_contable__tipo")
            .annotate(
                cargos_previos=Sum("cargo", filter=antes), abonos_previos=Sum("abono", filter=antes),
                cargos=Sum("cargo", filter=durante), abonos=Sum("abono", filter=durante),
            )
            .order_by("cuenta_contable__codigo", "cuenta_contable_id")
        )
        cuentas, t_cargos, t_abonos, acum_cargos, acum_abonos = [], CERO, CERO, CERO, CERO
        for f in filas:
            tipo = f["cuenta_contable__tipo"]
            cp, ap = Decimal(f["cargos_previos"] or 0), Decimal(f["abonos_previos"] or 0)
            c, a = Decimal(f["cargos"] or 0), Decimal(f["abonos"] or 0)
            t_cargos, t_abonos = t_cargos + c, t_abonos + a
            acum_cargos, acum_abonos = acum_cargos + cp + c, acum_abonos + ap + a
            cuentas.append({
                "cuenta_id": f["cuenta_contable_id"], "codigo": f["cuenta_contable__codigo"],
                "nombre": f["cuenta_contable__nombre"], "tipo": tipo, "naturaleza": _naturaleza(tipo),
                "saldo_inicial": _dos(_saldo(tipo, cp, ap)), "cargos": _dos(c), "abonos": _dos(a),
                "saldo_final": _dos(_saldo(tipo, cp + c, ap + a)),
            })
        return {
            "cuentas": cuentas,
            "total_cargos": _dos(t_cargos),
            "total_abonos": _dos(t_abonos),
            # Con todo lo contabilizado hasta fecha_fin, cargos y abonos deben ser iguales.
            "cuadre_correcto": abs(acum_cargos - acum_abonos) <= CENTAVO,
        }
