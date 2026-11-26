"""Motivo que escribe el usuario al cancelar, rechazar, retener o cerrar saldo."""
from rest_framework.exceptions import ValidationError

MOTIVO_MAX = 500


def exigir_largo_motivo(motivo: str, campo: str = "motivo") -> str:
    """``400 {campo}`` si pasa de ``MOTIVO_MAX`` caracteres. Antes cada módulo lo cortaba
    en silencio y el rastro guardaba un texto distinto al que escribió el usuario (#1399)."""
    if len(motivo) > MOTIVO_MAX:
        raise ValidationError({campo: f"El motivo admite hasta {MOTIVO_MAX} caracteres (tiene {len(motivo)})."})
    return motivo
