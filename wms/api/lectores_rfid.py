"""Lectores RFID administrables desde el ERP (WMS → RFID → Lectores RFID).

``/api/v1/wms/etiquetas-rfid/lectores/``: lista, alta y baja (soft delete) de
los ``LectorRFID`` de la empresa activa. Cada lector trae ``url_fx``, la URL
completa con su token para pegar en el IoT Connector del FX.

- Lectura: ``R-WMS-SCANNER`` (``LecturaPorCodigo``). Escritura: solo
  superusuario o ``is_admin_empresa``.
- Alcance: SIEMPRE la empresa activa, superusuario incluido (igual que
  ``scans/`` y ``scanner-stats``, EC-884); un no admin ve solo los lectores de
  sus sucursales (la misma regla que sus lecturas). Otra empresa → 404.
- Listado: 1 consulta (+1 de permisos y, si no es admin, +1 de sucursales),
  sin importar cuántos lectores haya; el estado sale de las dos marcas de la
  propia fila (``estado_lector``).
"""

from urllib.parse import quote

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import mixins, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from nucleo.listados import PaginacionOpcionalLimitOffset
from nucleo.models import Sucursal
from nucleo.permisos import LecturaPorCodigo
from wms.models import LectorRFID
from wms.services.rfid_scan_service import estado_lector

CODIGO_LECTURA = "R-WMS-SCANNER"
MSG_NOMBRE_DUPLICADO = "Ya existe un lector con este nombre en la empresa."


def _es_admin(user):
    return bool(getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False))


def url_fx(request, token):
    """URL del webhook para el IoT Connector, con la base de esta petición
    (``https://nucleo-erp.vercel.app`` en producción, ``http://localhost:8003``
    en local)."""
    base = reverse("rfid_receive_scan")
    if request is not None:
        base = request.build_absolute_uri(base)
    return f"{base}?token={quote(token, safe='')}"


class SoloAdminEscribe(BasePermission):
    message = "Solo un administrador de la empresa puede dar de alta o eliminar lectores."

    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or _es_admin(request.user)


class LectorRFIDSerializer(serializers.ModelSerializer):
    nombre = serializers.CharField(max_length=100, trim_whitespace=True)
    ubicacion = serializers.CharField(
        max_length=150, required=False, allow_blank=True, allow_null=True, default=""
    )
    # Opcional: sin ella, la sucursal por defecto del usuario. Define quién ve
    # las lecturas del lector (no admin: solo sus sucursales).
    sucursal = serializers.PrimaryKeyRelatedField(
        queryset=Sucursal.objects.all(), required=False
    )
    sucursal_nombre = serializers.CharField(source="sucursal.nombre", read_only=True, default=None)
    url_fx = serializers.SerializerMethodField()
    estado = serializers.SerializerMethodField()

    class Meta:
        model = LectorRFID
        fields = [
            "id",
            "nombre",
            "ubicacion",
            "sucursal",
            "sucursal_nombre",
            "url_fx",
            "estado",
            "ultima_conexion",
            "ultima_lectura",
            "created_at",
        ]
        read_only_fields = ["ultima_conexion", "ultima_lectura", "created_at"]

    def get_url_fx(self, obj) -> str:
        return url_fx(self.context.get("request"), obj.token)

    def get_estado(self, obj) -> str:
        return estado_lector(obj.ultima_conexion, obj.ultima_lectura, self.context.get("ahora"))

    def validate_ubicacion(self, value):
        return (value or "").strip()

    def validate(self, attrs):
        user = self.context["request"].user
        empresa_id = getattr(user, "empresa_id", None)
        if not empresa_id:
            raise ValidationError({"detail": "Selecciona una empresa activa para dar de alta lectores."})

        sucursal = attrs.get("sucursal")
        if sucursal is None and user.sucursal_default_id:
            sucursal = user.sucursal_default
        if sucursal is None or sucursal.empresa_id != empresa_id:
            raise ValidationError({"sucursal": "Indica una sucursal de la empresa activa."})
        attrs["sucursal"] = sucursal

        if LectorRFID.objects.filter(
            empresa_id=empresa_id, activo=True, nombre__iexact=attrs["nombre"]
        ).exists():
            raise ValidationError({"nombre": MSG_NOMBRE_DUPLICADO})
        attrs["empresa_id"] = empresa_id
        return attrs

    def create(self, validated_data):
        try:
            # Savepoint: la carrera de dos altas con el mismo nombre la resuelve
            # el índice único; aquí solo se traduce a 400.
            with transaction.atomic():
                return LectorRFID.objects.create(**validated_data)
        except IntegrityError:
            raise ValidationError({"nombre": MSG_NOMBRE_DUPLICADO})


class LectorRFIDViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet,
):
    serializer_class = LectorRFIDSerializer
    permission_classes = [IsAuthenticated, LecturaPorCodigo, SoloAdminEscribe]
    codigos_lectura = {"list": (CODIGO_LECTURA,), "retrieve": (CODIGO_LECTURA,)}
    pagination_class = PaginacionOpcionalLimitOffset
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        empresa_id = getattr(user, "empresa_id", None)
        if not empresa_id:
            return LectorRFID.objects.none()
        qs = (
            LectorRFID.objects.filter(empresa_id=empresa_id, activo=True)
            .select_related("sucursal")
            .order_by("nombre", "id")
        )
        if not _es_admin(user):
            qs = qs.filter(sucursal_id__in=user.sucursales_permitidas())
        return qs

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "ahora": timezone.now()}

    def destroy(self, request, *args, **kwargs):
        """Soft delete: el token deja de valer en ese momento (``receive-scan``
        solo acepta lectores activos) y sus lecturas se conservan."""
        lector = self.get_object()
        lector.activo = False
        lector.save(update_fields=["activo", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
