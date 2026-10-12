"""Alertas de mora de CxC y CxP: una por cuenta, en los hitos de 30, 60, 90 y 120 días vencida."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from finanzas.models import AlertaMora, CuentaPorCobrar, CuentaPorPagar

# (días vencida, nivel). Con menos del primer hito no hay alerta.
HITOS_MORA = (
    (30, AlertaMora.Nivel.LEVE),
    (60, AlertaMora.Nivel.MODERADA),
    (90, AlertaMora.Nivel.GRAVE),
    (120, AlertaMora.Nivel.CRITICA),
)


def _nivel_mora(dias: int):
    """El hito más alto alcanzado, o ``None`` si aún no llega al primero."""
    nivel = None
    for minimo, nivel_hito in HITOS_MORA:
        if dias >= minimo:
            nivel = nivel_hito
    return nivel


class AlertaMoraService:
    @staticmethod
    @transaction.atomic
    def generar_alertas(empresa=None, fecha_hoy=None):
        """Deja exactamente una alerta por cuenta vencida y regresa cuántas hay en el alcance.

        Crea las nuevas, actualiza ``dias_mora`` y ``nivel`` de las existentes y borra las de
        cuentas ya pagadas, canceladas o sin mora. ``notificado`` se conserva, salvo que el
        nivel suba: entonces hay que avisar de nuevo.
        """
        hoy = fecha_hoy or timezone.localdate()
        limite = hoy - timedelta(days=HITOS_MORA[0][0])
        for tipo, modelo, campo, estatus, empresa_de in (
            (AlertaMora.TipoCuenta.COBRAR, CuentaPorCobrar, "cuenta_por_cobrar",
             CuentaPorCobrar.EstatusCxC, "factura__empresa_id"),
            (AlertaMora.TipoCuenta.PAGAR, CuentaPorPagar, "cuenta_por_pagar",
             CuentaPorPagar.EstatusCxP, "factura_proveedor__empresa_id"),
        ):
            AlertaMoraService._sincronizar(
                tipo, modelo, campo, estatus, empresa_de, empresa, hoy, limite
            )
        alertas = AlertaMora.objects.filter(empresa=empresa) if empresa else AlertaMora.objects.all()
        return alertas.count()

    @staticmethod
    def _sincronizar(tipo, modelo, campo, estatus, empresa_de, empresa, hoy, limite):
        cuentas = (
            modelo.objects.exclude(estatus__in=[estatus.PAGADA, estatus.CANCELADA])
            .filter(saldo__gt=0, fecha_vencimiento__isnull=False, fecha_vencimiento__lte=limite)
        )
        if empresa:
            cuentas = cuentas.filter(empresa=empresa)
        deseadas = {}
        for pk, empresa_id, empresa_documento, vencimiento in cuentas.values_list(
            "pk", "empresa_id", empresa_de, "fecha_vencimiento"
        ):
            empresa_id = empresa_id or empresa_documento
            dias = (hoy - vencimiento).days
            nivel = _nivel_mora(dias)
            if empresa_id and nivel:
                deseadas[pk] = (empresa_id, dias, nivel)

        existentes = AlertaMora.objects.filter(tipo_cuenta=tipo)
        if empresa:
            existentes = existentes.filter(empresa=empresa)
        por_cuenta = {getattr(a, f"{campo}_id"): a for a in existentes}

        nuevas, cambiadas = [], []
        ahora = timezone.now()
        for pk, (empresa_id, dias, nivel) in deseadas.items():
            alerta = por_cuenta.get(pk)
            if alerta is None:
                nuevas.append(AlertaMora(
                    empresa_id=empresa_id, tipo_cuenta=tipo, dias_mora=dias, nivel=nivel,
                    fecha_generada=ahora, **{f"{campo}_id": pk},
                ))
            elif (alerta.dias_mora, alerta.nivel) != (dias, nivel):
                if alerta.nivel != nivel:
                    alerta.notificado, alerta.fecha_notificado = False, None
                alerta.dias_mora, alerta.nivel = dias, nivel
                cambiadas.append(alerta)

        AlertaMora.objects.filter(
            pk__in=[a.pk for pk, a in por_cuenta.items() if pk not in deseadas]
        ).delete()
        AlertaMora.objects.bulk_create(nuevas)
        AlertaMora.objects.bulk_update(cambiadas, ["dias_mora", "nivel", "notificado", "fecha_notificado"])
