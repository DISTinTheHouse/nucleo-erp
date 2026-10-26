from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from finanzas.exceptions import ErrorDeNegocio
from finanzas.services.cobro_poliza_service import CobroPolizaService
from finanzas.utils.folios import generate_cobro_folio
from finanzas.models import (
    Cobro,
    CobroDetalle,
    CuentaBancaria,
    CuentaPorCobrar,
    Factura,
    MovimientoBancario,
)
from terceros.models import Cliente


class CobroService:
    @staticmethod
    @transaction.atomic
    def aplicar_cobro(cobro: Cobro, usuario=None):
        Cobro.objects.select_for_update().get(pk=cobro.pk)  # #575
        detalles = list(
            CobroDetalle.objects.select_for_update()
            .select_related("cxc")
            .filter(cobro=cobro)
            .order_by("id")
        )
        if not detalles:
            raise ErrorDeNegocio(
                {"cobro_detalles": "El cobro debe tener al menos un detalle."}
            )
        # #645: el cliente y la cuenta EFECTIVOS del cobro, vengan o no en el PATCH.
        if not Cliente.objects.filter(pk=cobro.cliente_id, activo=True).exists():
            raise ErrorDeNegocio({"cliente": "El cliente está dado de baja."})

        cxc_ids = [d.cxc_id for d in detalles]
        if len(set(cxc_ids)) != len(cxc_ids):  # #586
            raise ErrorDeNegocio({"cobro_detalles": "Una CxC aparece dos veces en el cobro."})
        cxcs = {
            cxc.pk: cxc
            for cxc in CuentaPorCobrar.objects.select_for_update()
            .select_related("factura").filter(pk__in=cxc_ids)
        }
        moneda_cuenta, cuenta_activa = CuentaBancaria.objects.values_list("moneda_id", "activo").get(
            pk=cobro.cuenta_bancaria_id
        )
        if not cuenta_activa:
            raise ErrorDeNegocio({"cuenta_bancaria": "La cuenta bancaria está inactiva."})

        suma_aplicado = Decimal("0.00")
        for det in detalles:
            cxc = cxcs.get(det.cxc_id)
            if cxc is None:
                raise ErrorDeNegocio(
                    {"cobro_detalles": f"CxC {det.cxc_id} no encontrada."}
                )
            if cxc.cliente_id != cobro.cliente_id:  # #643: también si el cliente cambió por PATCH
                raise ErrorDeNegocio({"cobro_detalles": f"La CxC {cxc.pk} es de otro cliente."})
            if cxc.estatus == CuentaPorCobrar.EstatusCxC.CANCELADA:
                raise ErrorDeNegocio(
                    {"cobro_detalles": f"La CxC {cxc.pk} está cancelada y no admite cobros."}
                )
            factura_estatus = (
                Factura.objects.filter(pk=cxc.factura_id).values_list("estatus", flat=True).first()
                if cxc.factura_id
                else None
            )
            if cxc.factura.moneda_id != moneda_cuenta:  # #587
                raise ErrorDeNegocio(
                    {"cuenta_bancaria": f"La CxC {cxc.pk} está en otra moneda que la cuenta bancaria."}
                )
            if factura_estatus == Factura.FacturaStatus.CANCELADA:
                raise ErrorDeNegocio(
                    {"cobro_detalles": f"La factura de la CxC {cxc.pk} está cancelada y no admite cobros."}
                )
            if det.cxc.empresa_id and cobro.empresa_id and det.cxc.empresa_id != cobro.empresa_id:
                raise ErrorDeNegocio(
                    {"cobro_detalles": "CxC no pertenece a la misma empresa que el cobro."}
                )
            importe = Decimal(str(det.importe_aplicado or 0))
            if importe <= 0:
                raise ErrorDeNegocio(
                    {"cobro_detalles": "Cada importe aplicado debe ser mayor a 0."}
                )
            saldo_actual = Decimal(str(cxc.saldo or 0))
            if importe > saldo_actual + Decimal("0.0001"):
                raise ErrorDeNegocio(
                    {
                        "cobro_detalles": (
                            f"Importe {importe} excede saldo {saldo_actual} "
                            f"de la CxC {cxc.pk}."
                        )
                    }
                )
            suma_aplicado += importe

        total_cobrado = Decimal(str(cobro.total_cobrado or 0))
        if abs(suma_aplicado - total_cobrado) > Decimal("0.01"):
            raise ErrorDeNegocio(
                {
                    "total_cobrado": (
                        f"La suma de importes aplicados ({suma_aplicado}) "
                        f"debe coincidir con total_cobrado ({total_cobrado})."
                    )
                }
            )

        # #644: el folio se consume al aplicar (no en Borrador), con la sucursal de la
        # factura de la primera línea por id. Sin serie no se aplica (#662) y un folio
        # repetido en la empresa se rechaza (#663; la BD también lo impide).
        if not cobro.folio:
            sucursal = cxcs[detalles[0].cxc_id].factura.sucursal
            folio = generate_cobro_folio(cobro.empresa_id, sucursal.pk)
            if not folio:
                raise ErrorDeNegocio({"folio": f"La sucursal {sucursal.nombre} no tiene serie COBROS activa: dala de alta en series y folios."})
            if Cobro.objects.filter(empresa_id=cobro.empresa_id, folio=folio).exists():
                raise ErrorDeNegocio({"folio": f"El folio {folio} ya existe en otro cobro: revisa que las series COBROS de las sucursales no se repitan."})
            cobro.folio = folio
            cobro.save(update_fields=["folio"])

        hoy = timezone.localdate()
        for det in detalles:
            cxc = cxcs[det.cxc_id]
            importe = Decimal(str(det.importe_aplicado or 0))
            cxc.saldo = (Decimal(str(cxc.saldo or 0)) - importe).quantize(Decimal("0.01"))
            cxc.fecha_ultimo_pago = cobro.fecha_cobro or hoy  # #588
            if cxc.saldo <= Decimal("0.00"):
                cxc.saldo = Decimal("0.00")
                cxc.estatus = CuentaPorCobrar.EstatusCxC.PAGADA
            else:
                cxc.estatus = CuentaPorCobrar.EstatusCxC.PARCIAL
            cxc.save()

        cuenta = CuentaBancaria.objects.select_for_update().get(pk=cobro.cuenta_bancaria_id)
        if cobro.empresa_id and cuenta.empresa_id and cuenta.empresa_id != cobro.empresa_id:
            raise ErrorDeNegocio(
                {"cuenta_bancaria": "Cuenta bancaria no pertenece a la empresa."}
            )
        saldo_anterior = Decimal(str(cuenta.saldo_actual or 0))
        cuenta.saldo_actual = (saldo_anterior + total_cobrado).quantize(Decimal("0.01"))
        cuenta.save()

        mb = MovimientoBancario.objects.filter(cobro=cobro).first()
        if mb is None:
            MovimientoBancario.objects.create(
                cuenta_bancaria=cuenta,
                cobro=cobro,
                fecha=cobro.fecha_cobro or hoy,
                concepto=(f"Cobro {cobro.folio or cobro.pk} - Cliente {cobro.cliente_id}")[:255],
                referencia=(cobro.referencia_operacion or str(cobro.pk))[:100],
                importe=total_cobrado,
                saldo=cuenta.saldo_actual,
                origen=MovimientoBancario.OrigenOpciones.MANUAL,
                tipo_movimiento=MovimientoBancario.TipoMovimiento.ABONO,
                estatus=MovimientoBancario.Estatus.PENDIENTE,
            )
        else:
            mb.importe = total_cobrado
            mb.saldo = cuenta.saldo_actual
            mb.fecha = cobro.fecha_cobro or hoy
            mb.tipo_movimiento = MovimientoBancario.TipoMovimiento.ABONO
            mb.save()

        CobroPolizaService.generar(cobro, usuario)

    @staticmethod
    @transaction.atomic
    def cancelar_cobro(cobro: Cobro, usuario=None):
        # Bloqueo (#575): dos cancelaciones a la vez devolvían saldos dos veces.
        bloqueado = Cobro.objects.select_for_update().get(pk=cobro.pk)
        cobro.estatus = bloqueado.estatus
        if cobro.estatus == Cobro.Estatus.CANCELADO:
            return
        if bloqueado.rep_uuid or bloqueado.rep_pendiente:  # #577, #597
            raise ErrorDeNegocio({"rep": "El cobro tiene REP timbrado: cancélalo ante el SAT antes de cancelar el cobro."})
        if cobro.estatus != Cobro.Estatus.APLICADO:
            # Un Borrador nunca descontó saldos ni tocó el banco: revertirlos
            # inflaría la CxC y el saldo bancario con dinero que no entró.
            cobro.estatus = Cobro.Estatus.CANCELADO
            cobro.save()
            MovimientoBancario.objects.filter(cobro=cobro).update(
                estatus=MovimientoBancario.Estatus.CANCELADO
            )
            return
        detalles = list(CobroDetalle.objects.filter(cobro=cobro).select_related("cxc").all())
        cxc_ids = [d.cxc_id for d in detalles]
        cxcs = {
            cxc.pk: cxc
            for cxc in CuentaPorCobrar.objects.select_for_update().filter(pk__in=cxc_ids)
        }
        hoy = timezone.localdate()
        for det in detalles:
            cxc = cxcs.get(det.cxc_id)
            if cxc is None:
                continue
            importe = Decimal(str(det.importe_aplicado or 0))
            # #574/#603: la CxC de una factura cancelada sigue cancelada y con su saldo;
            # el último pago vuelve a ser el del cobro aplicado previo, si lo hay.
            if cxc.estatus != CuentaPorCobrar.EstatusCxC.CANCELADA:
                cxc.saldo = (Decimal(str(cxc.saldo or 0)) + importe).quantize(Decimal("0.01"))
                if cxc.saldo >= Decimal(str(cxc.total or 0)) - Decimal("0.01"):
                    cxc.estatus = CuentaPorCobrar.EstatusCxC.PENDIENTE
                else:
                    cxc.estatus = CuentaPorCobrar.EstatusCxC.PARCIAL
            cxc.fecha_ultimo_pago = (
                CobroDetalle.objects.filter(cxc=cxc, cobro__estatus=Cobro.Estatus.APLICADO, cobro__activo=True)
                .exclude(cobro=cobro).order_by("-cobro__fecha_cobro")
                .values_list("cobro__fecha_cobro", flat=True).first()
            )
            cxc.save()

        total_cobrado = Decimal(str(cobro.total_cobrado or 0))
        cuenta = CuentaBancaria.objects.select_for_update().get(pk=cobro.cuenta_bancaria_id)
        cuenta.saldo_actual = (
            Decimal(str(cuenta.saldo_actual or 0)) - total_cobrado
        ).quantize(Decimal("0.01"))
        cuenta.save()

        cobro.estatus = Cobro.Estatus.CANCELADO
        cobro.save()

        mb = MovimientoBancario.objects.filter(cobro=cobro).first()
        if mb:
            mb.estatus = MovimientoBancario.Estatus.CANCELADO
            mb.save()
        CobroPolizaService.cancelar(cobro, usuario)
