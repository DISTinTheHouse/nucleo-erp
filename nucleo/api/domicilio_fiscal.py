"""Domicilio fiscal de la empresa activa (#1053).

``GET  /api/v1/nucleo/empresa-actual/domicilio-fiscal/``: cualquier usuario de la empresa.
``PATCH``/``PUT`` mismo path: solo superusuario o admin de empresa.

Siempre la empresa ACTIVA del usuario (nunca un id del cliente). Sin empresa
activa responde 400.
"""
import re

from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Empresa
from ..paises import PAISES_ISO3

CAMPOS = (
    "calle", "numero_exterior", "numero_interior", "colonia", "localidad",
    "municipio", "estado", "pais", "codigo_postal",
)


class DomicilioFiscalSerializer(serializers.Serializer):
    calle = serializers.CharField(max_length=255, allow_blank=True, required=False)
    numero_exterior = serializers.CharField(max_length=30, allow_blank=True, required=False)
    numero_interior = serializers.CharField(max_length=30, allow_blank=True, required=False)
    colonia = serializers.CharField(max_length=120, allow_blank=True, required=False)
    localidad = serializers.CharField(max_length=120, allow_blank=True, required=False)
    municipio = serializers.CharField(max_length=120, allow_blank=True, required=False)
    estado = serializers.CharField(max_length=120, allow_blank=True, required=False)
    pais = serializers.CharField(max_length=3, allow_blank=True, required=False)
    codigo_postal = serializers.CharField(max_length=5, allow_blank=True, required=False)

    def validate_codigo_postal(self, value):
        value = (value or "").strip()
        if value and not re.fullmatch(r"\d{5}", value):
            raise serializers.ValidationError("El código postal debe tener 5 dígitos.")
        return value

    def validate_pais(self, value):
        value = (value or "").strip().upper()
        if value and value not in PAISES_ISO3:
            raise serializers.ValidationError("País inválido: usa la clave ISO de 3 letras (p. ej. MEX, USA).")
        return value


def _empresa(user):
    empresa_id = getattr(user, "empresa_id", None)
    if not empresa_id:
        raise ValidationError({"empresa": "El usuario no tiene empresa activa."})
    return Empresa.objects.only("pk", *(f"fiscal_{c}" for c in CAMPOS)).get(pk=empresa_id)


def _datos(empresa):
    return {c: getattr(empresa, f"fiscal_{c}") for c in CAMPOS}


class DomicilioFiscalAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_datos(_empresa(request.user)))

    def patch(self, request):
        return self._guardar(request, parcial=True)

    def put(self, request):
        return self._guardar(request, parcial=False)

    def _guardar(self, request, parcial):
        user = request.user
        if not (user.is_superuser or getattr(user, "is_admin_empresa", False)):
            raise PermissionDenied("Solo un administrador puede cambiar el domicilio fiscal.")
        empresa = _empresa(user)
        ser = DomicilioFiscalSerializer(data=request.data, partial=parcial)
        ser.is_valid(raise_exception=True)
        valores = ser.validated_data if parcial else {c: ser.validated_data.get(c, "") for c in CAMPOS}
        if not parcial and not valores.get("pais"):
            valores["pais"] = "MEX"
        for campo, valor in valores.items():
            setattr(empresa, f"fiscal_{campo}", valor.strip() if isinstance(valor, str) else valor)
        empresa.save(update_fields=[f"fiscal_{c}" for c in valores] + ["updated_at"])
        return Response(_datos(empresa), status=status.HTTP_200_OK)
