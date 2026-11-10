"""Serializers MFA de auth_kit que exigen código (EC-844).

Con ``MFA_DELETE_ACTIVE_METHOD_REQUIRE_CODE`` y
``MFA_UPDATE_PRIMARY_METHOD_REQUIRED_PRIMARY_CODE`` en ``True``, auth_kit 1.1.4
valida el código, pero declara ``code``/``primary_code`` como opcionales y luego
lee ``attrs["code"]``: si el cliente no lo manda, ``KeyError`` y 500. Aquí la
ausencia cuenta como código vacío, que el handler rechaza con 400.
"""

from auth_kit.mfa.serializers.mfa import (
    MFAMethodDeleteSerializer,
    MFAMethodPrimaryUpdateSerializer,
)


class BorrarMetodoMfaSerializer(MFAMethodDeleteSerializer):
    def validate(self, attrs):
        attrs.setdefault("code", "")
        return super().validate(attrs)


class CambiarMetodoPrimarioMfaSerializer(MFAMethodPrimaryUpdateSerializer):
    def validate(self, attrs):
        attrs.setdefault("primary_code", "")
        return super().validate(attrs)
