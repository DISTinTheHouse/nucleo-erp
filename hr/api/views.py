from django.db.models import ProtectedError

from rest_framework import mixins, status
from rest_framework.exceptions import APIException
from rest_framework.viewsets import GenericViewSet

from hr.models import (
    Puesto,
    Empleado,
    Area,
    Contrato,
    Turno,
    Calendario,
    Asistencia,
    ControlHoras,
    Vacaciones,
    PermisoAusencia,
    Incidencia,
    Evaluacion,
    Capacitacion,
    Nomina,
    Productividad,
)

from hr.api.serializers import (
    PuestoSerializer,
    EmpleadoSerializer,
    AreaSerializer,
    ContratoSerializer,
    TurnoSerializer,
    CalendarioSerializer,
    AsistenciaSerializer,
    ControlHorasSerializer,
    VacacionesSerializer,
    PermisoAusenciaSerializer,
    IncidenciaSerializer,
    EvaluacionSerializer,
    CapacitacionSerializer,
    NominaSerializer,
    ProductividadSerializer,
)

# Aislamiento multi-tenant de LECTURA en todos los ViewSets de HR.
#
# Sin estos ``get_queryset`` los ViewSets servían (y dejaban modificar/borrar)
# los registros de RH de todas las empresas. Se replica la convención ya usada
# en ``ventas``/``produccion``/``wms``/``terceros``: superuser ve todo; un
# usuario sin empresa asignada no ve nada (``qs.none()``); el resto sólo su
# empresa. Los modelos que no tienen FK ``empresa`` propia la heredan por la
# cadena de FK correspondiente (``empleado__empresa``, ``turno__empresa``,
# ``departamento__empresa``), igual que ``BomDetalleViewSet`` (``bom__empresa``)
# o ``PedidoDetalleTallaViewSet`` (``pedido_detalle__pedido__empresa``).
#
# El lado de ESCRITURA (validar que las FK enviadas en POST/PATCH pertenezcan a
# la empresa del usuario) vive en los serializers — ver ``hr/api/serializers.py``.
#
# Manejo de DELETE en HR (ver los dos mixins de abajo):
#
# * ``Puesto``, ``Empleado`` y ``Area`` heredan ``StatusLifecycleModel`` y se
#   dan de baja con ``soft_delete()`` — la convención del repo para datos de
#   catálogo/tenant. Antes ``DestroyModelMixin`` los borraba físicamente.
# * El resto de modelos no tiene ciclo de vida, así que sí se borran; pero como
#   todas las FK entrantes son ``on_delete=PROTECT``, un borrado con
#   dependencias lanzaba ``ProtectedError`` —que no es ``APIException``— y
#   salía como 500. Ahora se traduce a 409.


class RegistroConDependenciasError(APIException):
    """409 cuando una FK ``PROTECT`` impide borrar el registro."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "No se puede eliminar el registro porque existen registros relacionados "
        "que dependen de él."
    )
    default_code = "registro_con_dependencias"


class ProtectedDestroyMixin:
    """Traduce ``ProtectedError`` a 409 en vez de dejarlo escalar a 500.

    Debe declararse ANTES de ``mixins.DestroyModelMixin`` para que el ``super()``
    resuelva al ``perform_destroy`` de DRF.
    """

    def perform_destroy(self, instance):
        try:
            super().perform_destroy(instance)
        except ProtectedError as exc:
            raise RegistroConDependenciasError(
                self._mensaje_dependencias(exc)
            ) from exc

    @staticmethod
    def _mensaje_dependencias(exc):
        # ``protected_objects`` ya viene materializado por el colector de
        # Django, así que nombrar los modelos que bloquean no cuesta consultas.
        modelos = sorted({
            str(obj._meta.verbose_name_plural)
            for obj in exc.protected_objects
        })
        if not modelos:
            return RegistroConDependenciasError.default_detail
        return (
            "No se puede eliminar el registro porque tiene dependencias en: "
            f"{', '.join(modelos)}."
        )


class SoftDeleteDestroyMixin:
    """``DELETE`` sobre modelos ``StatusLifecycleModel`` = baja lógica.

    Marca ``activo = False`` vía ``soft_delete()`` en lugar de borrar la fila,
    y devuelve el 204 habitual de ``DestroyModelMixin``.
    """

    def perform_destroy(self, instance):
        instance.soft_delete()


class PuestoViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet

):
    queryset = Puesto.objects.all()
    serializer_class = PuestoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empresa", "area", "area__departamento")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empresa=empresa)

class EmpleadoViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Empleado.objects.all()
    serializer_class = EmpleadoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empresa", "sucursal", "departamento", "puesto"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empresa=empresa)

class AreaViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Area.objects.all()
    serializer_class = AreaSerializer

    def get_queryset(self):
        # ``Area`` no tiene FK ``empresa``: la hereda por ``departamento``.
        user = self.request.user
        qs = super().get_queryset().select_related(
            "departamento", "departamento__empresa", "responsable"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(departamento__empresa=empresa)

class ContratoViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Contrato.objects.all()
    serializer_class = ContratoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empleado", "empleado__empresa")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

class TurnoViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Turno.objects.all()
    serializer_class = TurnoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empresa")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empresa=empresa)

class CalendarioViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Calendario.objects.all()
    serializer_class = CalendarioSerializer

    def get_queryset(self):
        # ``Calendario`` hereda la empresa por ``turno``.
        user = self.request.user
        qs = super().get_queryset().select_related("turno", "turno__empresa")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(turno__empresa=empresa)

class AsistenciaViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Asistencia.objects.all()
    serializer_class = AsistenciaSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empleado", "empleado__empresa", "turno"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

class ControlHorasViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = ControlHoras.objects.all()
    serializer_class = ControlHorasSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empleado", "empleado__empresa", "asistencia", "op"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

class VacacionesViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Vacaciones.objects.all()
    serializer_class = VacacionesSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empleado", "empleado__empresa")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

class PermisoAusenciaViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = PermisoAusencia.objects.all()
    serializer_class = PermisoAusenciaSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empleado", "empleado__empresa")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

class IncidenciaViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Incidencia.objects.all()
    serializer_class = IncidenciaSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empleado", "empleado__empresa")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

class EvaluacionViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Evaluacion.objects.all()
    serializer_class = EvaluacionSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empleado", "empleado__empresa", "evaluador"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

class CapacitacionViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Capacitacion.objects.all()
    serializer_class = CapacitacionSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empleado", "empleado__empresa")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

class NominaViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Nomina.objects.all()
    serializer_class = NominaSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empresa", "sucursal", "empleado")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empresa=empresa)

class ProductividadViewSet(
    ProtectedDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Productividad.objects.all()
    serializer_class = ProductividadSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empresa", "departamento", "empleado", "meta_unidad"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empresa=empresa)
