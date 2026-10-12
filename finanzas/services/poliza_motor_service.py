"""Genera la póliza automática de un documento.

Cada documento arma sus líneas con conceptos contables (``ConceptoContable``) y
este motor resuelve las cuentas, folia y guarda. Costo fijo por póliza: una
consulta para las cuentas, una para el centro de costo, el folio y un solo
``bulk_create``; el cuadre se calcula en memoria.
"""

from decimal import Decimal

from django.db import IntegrityError, transaction

from finanzas.models import Poliza, PolizaDetalle
from finanzas.services.plan_contable_service import PlanContableService
from finanzas.services.poliza_service import CENTAVO, PolizaService
from finanzas.utils.folios import generate_poliza_folio


def _origen(origen_tipo, origen_id):
    return Poliza.objects.filter(
        origen_tipo=origen_tipo, origen_id=origen_id, poliza_reversa_de__isnull=True
    ).first()


class PolizaMotorService:
    @staticmethod
    def generar(
        *, empresa_id, sucursal_id, origen_tipo, origen_id, fecha, tipo, concepto, lineas, usuario=None
    ):
        """Crea la póliza Contabilizada del documento y la regresa.

        ``lineas``: lista de dicts con ``concepto`` (``ConceptoContable``, más
        ``clave`` opcional) o ``cuenta`` ya resuelta, ``cargo`` o ``abono``, y
        opcionalmente ``referencia``, ``observaciones`` y las FK del documento
        (``factura``, ``cobro``, ...).

        Regresa ``None`` si la empresa no contabiliza (interruptor apagado o
        documento anterior a la fecha de inicio). Si el documento ya tiene
        póliza, regresa esa.
        """
        if not PlanContableService.contabilizacion_activa(empresa_id, fecha=fecha):
            return None
        existente = _origen(origen_tipo, origen_id)
        if existente:
            return existente
        try:
            with transaction.atomic():
                return PolizaMotorService._crear(
                    empresa_id, sucursal_id, origen_tipo, origen_id, fecha, tipo, concepto, lineas, usuario
                )
        except IntegrityError:
            # Otra petición contabilizó el mismo documento al mismo tiempo.
            existente = _origen(origen_tipo, origen_id)
            if existente is None:
                raise
            return existente

    @staticmethod
    def resolver(empresa_id, lineas):
        """Resuelve y valida las cuentas de las líneas, sin guardar nada.

        Regresa ``[(linea, cuenta, centro_costo, cargo, abono)]`` sin los
        renglones en ceros (p. ej. IVA en una factura sin impuestos) y el
        centro de costo por omisión de la empresa.
        """
        pares = {(l["concepto"], l.get("clave", "")) for l in lineas if "concepto" in l}
        mapa = PlanContableService.exigir(empresa_id, pares) if pares else {}
        centro_default = PlanContableService.centro_costo_default(empresa_id)

        resueltas = []
        for linea in lineas:
            cargo = Decimal(str(linea.get("cargo") or 0)).quantize(CENTAVO)
            abono = Decimal(str(linea.get("abono") or 0)).quantize(CENTAVO)
            if cargo == 0 and abono == 0:
                continue
            if "cuenta" in linea:
                cuenta, centro = linea["cuenta"], centro_default
            else:
                config = mapa[(linea["concepto"], linea.get("clave", ""))]
                cuenta, centro = config.cuenta_contable, config.centro_costo or centro_default
            PlanContableService.validar_cuenta(cuenta, empresa_id)
            resueltas.append((linea, cuenta, centro, cargo, abono))
        return resueltas, centro_default

    @staticmethod
    def previsualizar(empresa_id, lineas):
        """El asiento que se generaría, sin guardar y sin importar el interruptor."""
        resueltas, _ = PolizaMotorService.resolver(empresa_id, lineas)
        renglones = [
            {
                "cuenta_contable_id": cuenta.pk,
                "cuenta_codigo": cuenta.codigo,
                "cuenta_nombre": cuenta.nombre,
                "cargo": str(cargo),
                "abono": str(abono),
                "referencia": linea.get("referencia"),
            }
            for linea, cuenta, _, cargo, abono in resueltas
        ]
        total_cargos = sum((r[3] for r in resueltas), Decimal("0.00"))
        total_abonos = sum((r[4] for r in resueltas), Decimal("0.00"))
        return {
            "renglones": renglones,
            "total_cargos": str(total_cargos),
            "total_abonos": str(total_abonos),
            "cuadre_correcto": abs(total_cargos - total_abonos) <= CENTAVO,
        }

    @staticmethod
    def _crear(empresa_id, sucursal_id, origen_tipo, origen_id, fecha, tipo, concepto, lineas, usuario):
        resueltas, centro_default = PolizaMotorService.resolver(empresa_id, lineas)

        folio, consecutivo = generate_poliza_folio(empresa_id, sucursal_id)
        poliza = Poliza.objects.create(
            empresa_id=empresa_id,
            sucursal_id=sucursal_id,
            centro_costo=centro_default,
            folio=folio,
            folio_consecutivo=consecutivo,
            tipo=tipo,
            fecha=fecha,
            concepto=concepto[:200],
            estatus=Poliza.PolizaStatus.CONTABILIZADA,
            usuario_creacion=usuario,
            origen_tipo=origen_tipo,
            origen_id=origen_id,
        )
        detalles = [
            PolizaDetalle(
                poliza=poliza,
                cuenta_contable=cuenta,
                centro_costo=centro,
                cargo=cargo,
                abono=abono,
                referencia=linea.get("referencia"),
                observaciones=linea.get("observaciones"),
                orden=orden,
                **{k: linea[k] for k in (
                    "factura", "factura_proveedor", "pago", "cobro", "movimiento_bancario"
                ) if k in linea},
            )
            for orden, (linea, cuenta, centro, cargo, abono) in enumerate(resueltas, start=1)
        ]
        PolizaService.validar_suma_cero(poliza, detalles)
        PolizaDetalle.objects.bulk_create(detalles)
        return poliza
