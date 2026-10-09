"""Resolución de conceptos contables a cuentas del catálogo de cada empresa.

Sustituye al placeholder que elegía "la primera cuenta activa del tipo X
ordenada por código": si una empresa tenía ``1000 Caja`` antes que
``1200 Clientes``, el cargo de clientes se iba a Caja, y el IVA podía aterrizar
en cualquier pasivo.
"""

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import (
    CentroCosto,
    ConceptoContable,
    ConfiguracionContable,
    ParametrosContabilidad,
)


class PlanContableService:
    @staticmethod
    def parametros(empresa_id):
        """Interruptores de la empresa. Si no tiene fila, cuenta como apagada."""
        return ParametrosContabilidad.objects.filter(empresa_id=empresa_id).first()

    @staticmethod
    def contabilizacion_activa(empresa_id, *, fecha=None):
        """¿Hay que contabilizar este documento?

        Son dos condiciones y las dos deben cumplirse: el interruptor encendido
        y, si hay piso de fecha, que el documento no sea anterior. Tener el
        catálogo sembrado no basta: se siembra para revisarlo con la generación
        apagada.
        """
        parametros = PlanContableService.parametros(empresa_id)
        if parametros is None or not parametros.contabilizacion_automatica:
            return False
        piso = parametros.fecha_inicio_contabilizacion
        if piso is not None and fecha is not None and fecha < piso:
            return False
        return True

    @staticmethod
    def mapa(empresa_id, pares):
        """``{(concepto, clave): ConfiguracionContable}`` en UNA sola consulta.

        ``pares`` es un iterable de ``(concepto, clave)``. Una clave que no esté
        dada de alta cae a la regla por omisión del concepto (``clave=""``), que
        es lo que permite mandar todas las percepciones de nómina a una sola
        cuenta con una fila de configuración.
        """
        pares = list(pares)
        conceptos = {concepto for concepto, _ in pares}
        filas = (
            ConfiguracionContable.objects
            .filter(empresa_id=empresa_id, concepto__in=conceptos, activo=True)
            .select_related("cuenta_contable", "centro_costo")
        )
        por_concepto_y_clave = {(f.concepto, f.clave or ""): f for f in filas}

        resuelto = {}
        for concepto, clave in pares:
            clave = clave or ""
            fila = (
                por_concepto_y_clave.get((concepto, clave))
                or por_concepto_y_clave.get((concepto, ""))
            )
            if fila is not None:
                resuelto[(concepto, clave)] = fila
        return resuelto

    @staticmethod
    def exigir(empresa_id, pares):
        """Como ``mapa``, pero falla si falta alguno.

        Acumula TODOS los faltantes en un solo error: configurar el plan a
        base de reintentos, descubriendo una cuenta ausente por vez, es un
        suplicio innecesario.
        """
        resuelto = PlanContableService.mapa(empresa_id, pares)
        faltantes = [
            ConceptoContable(concepto).label
            for concepto, clave in pares
            if (concepto, clave or "") not in resuelto
        ]
        if faltantes:
            raise ErrorDeNegocio({
                "configuracion_contable": (
                    "Faltan cuentas configuradas para esta empresa: "
                    + ", ".join(sorted(set(faltantes)))
                    + "."
                )
            })
        return resuelto

    @staticmethod
    def cuenta(empresa_id, concepto, *, clave="", requerida=True):
        """La cuenta de un concepto suelto. ``None`` si no está y no es requerida."""
        resuelto = PlanContableService.mapa(empresa_id, [(concepto, clave)])
        fila = resuelto.get((concepto, clave or ""))
        if fila is None:
            if requerida:
                PlanContableService.exigir(empresa_id, [(concepto, clave)])
            return None
        return fila.cuenta_contable

    @staticmethod
    def cuenta_de_banco(cuenta_bancaria):
        """La del propio banco; si no tiene, el concepto ``BANCOS``."""
        cuenta = getattr(cuenta_bancaria, "cuenta_contable", None)
        if cuenta is not None:
            return cuenta
        cuenta = PlanContableService.cuenta(
            cuenta_bancaria.empresa_id, ConceptoContable.BANCOS, requerida=False
        )
        if cuenta is None:
            raise ErrorDeNegocio({
                "cuenta_contable": (
                    f"La cuenta bancaria '{cuenta_bancaria.alias or cuenta_bancaria.pk}' no "
                    "tiene cuenta contable y la empresa no configuró el concepto Bancos."
                )
            })
        return cuenta

    @staticmethod
    def centro_costo_default(empresa_id):
        """Opcional: ``Poliza.centro_costo`` admite nulo y no debe bloquear nada."""
        return (
            CentroCosto.objects.filter(empresa_id=empresa_id, activo=True)
            .order_by("codigo", "id")
            .first()
        )

    @staticmethod
    def validar_cuenta(cuenta, empresa_id, *, campo="cuenta_contable"):
        """Relectura de las reglas en el momento de contabilizar.

        El serializer ya las aplica al configurar, pero la cuenta pudo darse de
        baja *después*: sin esto el asiento saldría contra una cuenta muerta.
        """
        if cuenta is None:
            raise ErrorDeNegocio({campo: "Falta la cuenta contable."})
        if cuenta.empresa_id != empresa_id:
            raise ErrorDeNegocio({campo: f"La cuenta {cuenta.codigo} es de otra empresa."})
        if not cuenta.activo:
            raise ErrorDeNegocio({campo: f"La cuenta {cuenta.codigo} está dada de baja."})
        if not cuenta.acepta_movimientos:
            raise ErrorDeNegocio({
                campo: f"La cuenta {cuenta.codigo} es de agrupación: no acepta movimientos."
            })
        return cuenta
