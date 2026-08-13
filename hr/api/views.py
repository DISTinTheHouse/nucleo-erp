from rest_framework import mixins
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

class PuestoViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet

):
    queryset = Puesto.objects.all()
    serializer_class = PuestoSerializer

class EmpleadoViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Empleado.objects.all()
    serializer_class = EmpleadoSerializer

class AreaViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Area.objects.all()
    serializer_class = AreaSerializer

class ContratoViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Contrato.objects.all()
    serializer_class = ContratoSerializer

class TurnoViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Turno.objects.all()
    serializer_class = TurnoSerializer

class CalendarioViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Calendario.objects.all()
    serializer_class = CalendarioSerializer

class AsistenciaViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Asistencia.objects.all()
    serializer_class = AsistenciaSerializer

class ControlHorasViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = ControlHoras.objects.all()
    serializer_class = ControlHorasSerializer

class VacacionesViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Vacaciones.objects.all()
    serializer_class = VacacionesSerializer

class PermisoAusenciaViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = PermisoAusencia.objects.all()
    serializer_class = PermisoAusenciaSerializer

class IncidenciaViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Incidencia.objects.all()
    serializer_class = IncidenciaSerializer

class EvaluacionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Evaluacion.objects.all()
    serializer_class = EvaluacionSerializer

class CapacitacionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Capacitacion.objects.all()
    serializer_class = CapacitacionSerializer

class NominaViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Nomina.objects.all()
    serializer_class = NominaSerializer

class ProductividadViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Productividad.objects.all()
    serializer_class = ProductividadSerializer