from django.db.models import ProtectedError, Sum, Count, Q, F, Value, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone
from datetime import date, datetime, timedelta
from decimal import Decimal

from rest_framework import mixins, status, filters
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet
from django_filters.rest_framework import DjangoFilterBackend

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
    NominaDetalle,
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


FILTER_BACKENDS = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]


class RegistroConDependenciasError(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = (
        "No se puede eliminar el registro porque existen registros relacionados "
        "que dependen de él."
    )
    default_code = "registro_con_dependencias"


class SoftDeleteDestroyMixin:
    """
    Siempre soft-delete. Orden de prioridad:
    1) instance.soft_delete() (StatusLifecycleModel)
    2) instance.activo = False + save() (si tiene campo 'activo')
    3) instance.delete() con ProtectedError → 409 (último recurso, no rompe)
    """

    def perform_destroy(self, instance):
        if hasattr(instance, "soft_delete") and callable(getattr(instance, "soft_delete")):
            instance.soft_delete()
            return
        if hasattr(instance, "activo"):
            instance.activo = False
            instance.save(update_fields=["activo", "actualizado_en"] if hasattr(instance, "actualizado_en") else ["activo"])
            return
        try:
            instance.delete()
        except ProtectedError as exc:
            raise RegistroConDependenciasError(self._mensaje_dependencias(exc)) from exc

    @staticmethod
    def _mensaje_dependencias(exc):
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
    filter_backends = FILTER_BACKENDS
    filterset_fields = ['area', 'activo', 'empresa']
    search_fields = ['nombre', 'descripcion']
    ordering_fields = ['nombre', 'salario_base']
    ordering = ['nombre']

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
    filter_backends = FILTER_BACKENDS
    filterset_fields = ['sucursal', 'departamento', 'puesto', 'turno', 'activo', 'sexo', 'estado_civil']
    search_fields = ['nombre', 'apellido_paterno', 'apellido_materno', 'numero_empleado', 'curp', 'rfc', 'email', 'telefono', 'nss']
    ordering_fields = ['numero_empleado', 'nombre', 'apellido_paterno', 'fecha_ingreso', 'fecha_nacimiento']
    ordering = ['apellido_paterno', 'nombre']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empresa", "sucursal", "departamento", "puesto", "turno"
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
    filter_backends = FILTER_BACKENDS
    filterset_fields = ['departamento', 'activo', 'responsable']
    search_fields = ['nombre', 'codigo', 'descripcion']
    ordering_fields = ['nombre', 'codigo']
    ordering = ['nombre']

    def get_queryset(self):
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
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Contrato.objects.all()
    serializer_class = ContratoSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'tipo': ['exact'],
        'estado': ['exact'],
        'activo': ['exact'],
        'fecha_inicio': ['gte', 'lte'],
        'fecha_fin': ['gte', 'lte'],
    }
    search_fields = ['archivo_url', 'observaciones', 'prestaciones']
    ordering_fields = ['fecha_inicio', 'fecha_fin', 'salario']
    ordering = ['-fecha_inicio']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("empleado", "empleado__empresa", "creado_por")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

    def perform_create(self, serializer):
        serializer.save(creado_por=self.request.user)


class TurnoViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Turno.objects.all()
    serializer_class = TurnoSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = ['activo', 'empresa', 'dias_laborales']
    search_fields = ['nombre', 'descripcion']
    ordering_fields = ['nombre', 'hora_entrada', 'hora_salida', 'horas_base_diarias']
    ordering = ['nombre']

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
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Calendario.objects.all()
    serializer_class = CalendarioSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'turno': ['exact'],
        'tipo': ['exact'],
        'fecha': ['gte', 'lte'],
    }
    search_fields = ['tipo', 'turno__nombre']
    ordering_fields = ['fecha', 'tipo']
    ordering = ['-fecha']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related("turno", "turno__empresa")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(turno__empresa=empresa)


class AsistenciaViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Asistencia.objects.all()
    serializer_class = AsistenciaSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'turno': ['exact'],
        'estado': ['exact'],
        'fecha': ['exact', 'gte', 'lte'],
    }
    search_fields = ['observaciones', 'empleado__nombre', 'empleado__numero_empleado']
    ordering_fields = ['fecha', 'hora_entrada', 'hora_salida', 'minutos_retardo']
    ordering = ['-fecha']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empleado", "empleado__empresa", "empleado__sucursal", "empleado__departamento", "turno", "autorizado_por"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

    @action(detail=False, methods=['POST'])
    def registrar_entrada(self, request):
        empleado_id = request.data.get('empleado_id')
        if not empleado_id:
            return Response({'empleado_id': ['Este campo es requerido.']}, status=status.HTTP_400_BAD_REQUEST)
        user = request.user
        empleados_qs = Empleado.objects.all()
        if getattr(user, "is_superuser", False):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)
            empleados_qs = empleados_qs.filter(empresa=empresa)
        try:
            empleado = empleados_qs.get(pk=empleado_id)
        except Empleado.DoesNotExist:
            return Response({'detail': 'Empleado no encontrado.'}, status=status.HTTP_404_NOT_FOUND)

        fecha_str = request.data.get('fecha')
        if fecha_str:
            try:
                fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'detail': 'Formato de fecha inválido (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            fecha = date.today()

        hora_str = request.data.get('hora')
        if hora_str:
            try:
                ahora = datetime.strptime(hora_str, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return Response({'detail': 'Formato de hora inválido (YYYY-MM-DD HH:MM:SS).'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            ahora = timezone.now()

        turno = empleado.turno
        if not turno:
            return Response({'detail': 'El empleado no tiene turno asignado.'}, status=status.HTTP_400_BAD_REQUEST)

        asistencia, created = Asistencia.objects.get_or_create(
            empleado=empleado,
            fecha=fecha,
            defaults={'turno': turno, 'hora_entrada': ahora, 'estado': 'puntual'}
        )
        if not created:
            asistencia.hora_entrada = ahora
            asistencia.turno = turno
        asistencia.save()
        serializer = self.get_serializer(asistencia)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['POST'])
    def registrar_salida(self, request):
        empleado_id = request.data.get('empleado_id')
        if not empleado_id:
            return Response({'empleado_id': ['Este campo es requerido.']}, status=status.HTTP_400_BAD_REQUEST)
        user = request.user
        empleados_qs = Empleado.objects.all()
        if getattr(user, "is_superuser", False):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)
            empleados_qs = empleados_qs.filter(empresa=empresa)
        try:
            empleado = empleados_qs.get(pk=empleado_id)
        except Empleado.DoesNotExist:
            return Response({'detail': 'Empleado no encontrado.'}, status=status.HTTP_404_NOT_FOUND)

        fecha_str = request.data.get('fecha')
        if fecha_str:
            try:
                fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'detail': 'Formato de fecha inválido (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            fecha = date.today()

        hora_str = request.data.get('hora')
        if hora_str:
            try:
                ahora = datetime.strptime(hora_str, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return Response({'detail': 'Formato de hora inválido (YYYY-MM-DD HH:MM:SS).'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            ahora = timezone.now()

        asistencias_qs = Asistencia.objects.filter(empleado=empleado, fecha=fecha)
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if empresa:
                asistencias_qs = asistencias_qs.filter(empleado__empresa=empresa)
        try:
            asistencia = asistencias_qs.get()
        except Asistencia.DoesNotExist:
            return Response({'detail': 'No se encontró registro de entrada para esta fecha.'}, status=status.HTTP_404_NOT_FOUND)

        asistencia.hora_salida = ahora
        asistencia.save()
        serializer = self.get_serializer(asistencia)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ControlHorasViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = ControlHoras.objects.all()
    serializer_class = ControlHorasSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'tipo': ['exact'],
        'op': ['exact'],
        'fecha': ['exact', 'gte', 'lte'],
    }
    search_fields = ['descripcion', 'empleado__numero_empleado']
    ordering_fields = ['fecha', 'hora_inicio', 'hora_fin', 'horas_trabajadas']
    ordering = ['-fecha', '-hora_inicio']

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
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Vacaciones.objects.all()
    serializer_class = VacacionesSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'estado': ['exact'],
        'fecha_inicio': ['gte'],
        'fecha_fin': ['lte'],
        'fecha_solicitud': ['gte', 'lte'],
    }
    search_fields = ['motivo', 'motivo_rechazo']
    ordering_fields = ['fecha_solicitud', 'fecha_inicio', 'fecha_fin', 'dias_solicitados']
    ordering = ['-fecha_solicitud']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empleado", "empleado__empresa", "solicitado_por", "autorizado_por", "rechazado_por"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

    def perform_create(self, serializer):
        serializer.save(solicitado_por=self.request.user)

    @action(detail=True, methods=['POST'])
    def aprobar(self, request, pk=None):
        obj = self.get_object()
        user = request.user
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if not empresa or obj.empleado.empresa_id != empresa.pk:
                return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)
        if obj.estado != 'pendiente':
            return Response({'detail': 'Solo se pueden aprobar solicitudes pendientes.'}, status=status.HTTP_400_BAD_REQUEST)
        obj.estado = 'aprobado'
        obj.autorizado_por = request.user
        obj.fecha_aprobacion = timezone.now()
        obj.save()
        serializer = self.get_serializer(obj)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['POST'])
    def rechazar(self, request, pk=None):
        obj = self.get_object()
        user = request.user
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if not empresa or obj.empleado.empresa_id != empresa.pk:
                return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)
        if obj.estado != 'pendiente':
            return Response({'detail': 'Solo se pueden rechazar solicitudes pendientes.'}, status=status.HTTP_400_BAD_REQUEST)
        motivo = request.data.get('motivo_rechazo', '')
        obj.estado = 'rechazado'
        obj.rechazado_por = request.user
        obj.fecha_rechazo = timezone.now()
        obj.motivo_rechazo = motivo
        obj.save()
        serializer = self.get_serializer(obj)
        return Response(serializer.data, status=status.HTTP_200_OK)


class PermisoAusenciaViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = PermisoAusencia.objects.all()
    serializer_class = PermisoAusenciaSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'tipo': ['exact'],
        'estado': ['exact'],
        'con_goce_sueldo': ['exact'],
        'fecha_inicio': ['gte'],
        'fecha_fin': ['lte'],
        'fecha_solicitud': ['gte', 'lte'],
    }
    search_fields = ['motivo', 'motivo_rechazo']
    ordering_fields = ['fecha_solicitud', 'fecha_inicio', 'fecha_fin']
    ordering = ['-fecha_solicitud']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empleado", "empleado__empresa", "solicitado_por", "autorizado_por", "rechazado_por"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

    def perform_create(self, serializer):
        serializer.save(solicitado_por=self.request.user)

    @action(detail=True, methods=['POST'])
    def aprobar(self, request, pk=None):
        obj = self.get_object()
        user = request.user
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if not empresa or obj.empleado.empresa_id != empresa.pk:
                return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)
        if obj.estado != 'pendiente':
            return Response({'detail': 'Solo se pueden aprobar solicitudes pendientes.'}, status=status.HTTP_400_BAD_REQUEST)
        obj.estado = 'aprobado'
        obj.autorizado_por = request.user
        obj.fecha_aprobacion = timezone.now()
        obj.save()
        serializer = self.get_serializer(obj)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['POST'])
    def rechazar(self, request, pk=None):
        obj = self.get_object()
        user = request.user
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if not empresa or obj.empleado.empresa_id != empresa.pk:
                return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)
        if obj.estado != 'pendiente':
            return Response({'detail': 'Solo se pueden rechazar solicitudes pendientes.'}, status=status.HTTP_400_BAD_REQUEST)
        motivo = request.data.get('motivo_rechazo', '')
        obj.estado = 'rechazado'
        obj.rechazado_por = request.user
        obj.fecha_rechazo = timezone.now()
        obj.motivo_rechazo = motivo
        obj.save()
        serializer = self.get_serializer(obj)
        return Response(serializer.data, status=status.HTTP_200_OK)


class IncidenciaViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Incidencia.objects.all()
    serializer_class = IncidenciaSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'tipo': ['exact'],
        'gravedad': ['exact'],
        'estado': ['exact'],
        'activo': ['exact'],
        'fecha': ['exact', 'gte', 'lte'],
    }
    search_fields = ['descripcion', 'acciones_tomadas']
    ordering_fields = ['fecha', 'fecha_reporte', 'gravedad', 'estado']
    ordering = ['-fecha', '-fecha_reporte']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empleado", "empleado__empresa", "reportado_por"
        )
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empleado__empresa=empresa)

    def perform_create(self, serializer):
        serializer.save(reportado_por=self.request.user)


class EvaluacionViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Evaluacion.objects.all()
    serializer_class = EvaluacionSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'evaluador': ['exact'],
        'tipo': ['exact'],
        'periodo': ['exact'],
        'estado': ['exact'],
        'fecha': ['gte', 'lte'],
        'puntaje': ['gte', 'lte'],
    }
    search_fields = ['comentarios', 'empleado__nombre', 'evaluador__nombre']
    ordering_fields = ['fecha', 'puntaje', 'periodo']
    ordering = ['-fecha']

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
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Capacitacion.objects.all()
    serializer_class = CapacitacionSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'estado': ['exact'],
        'institucion': ['exact'],
        'fecha_inicio': ['gte', 'lte'],
        'fecha_fin': ['gte', 'lte'],
        'calificacion': ['gte', 'lte'],
    }
    search_fields = ['nombre', 'institucion', 'constancia_url']
    ordering_fields = ['fecha_inicio', 'fecha_fin', 'horas', 'calificacion']
    ordering = ['-fecha_inicio']

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
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Nomina.objects.all()
    serializer_class = NominaSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'sucursal': ['exact'],
        'estado': ['exact'],
        'periodo_inicio': ['gte', 'lte'],
        'periodo_fin': ['gte', 'lte'],
        'fecha_pago': ['gte', 'lte'],
    }
    search_fields = ['observaciones', 'empleado__numero_empleado', 'empleado__nombre']
    ordering_fields = ['periodo_inicio', 'fecha_pago', 'neto', 'total_percepciones', 'total_deducciones']
    ordering = ['-periodo_inicio', '-fecha_generacion']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empresa", "sucursal", "empleado", "creado_por"
        ).prefetch_related("detalles")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empresa=empresa)

    @action(detail=True, methods=['POST'])
    def calcular_totales(self, request, pk=None):
        obj = self.get_object()
        user = request.user
        if not getattr(user, "is_superuser", False):
            empresa = getattr(user, "empresa", None)
            if not empresa or obj.empresa_id != empresa.pk:
                return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)
        obj._recalcular_totales()
        serializer = self.get_serializer(obj)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['POST'])
    def generar_periodo(self, request):
        periodo_inicio = request.data.get('periodo_inicio')
        periodo_fin = request.data.get('periodo_fin')
        sucursal_id = request.data.get('sucursal_id')
        fecha_pago = request.data.get('fecha_pago')

        if not periodo_inicio or not periodo_fin:
            return Response({
                'periodo_inicio': ['Este campo es requerido.'],
                'periodo_fin': ['Este campo es requerido.'],
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            pi = datetime.strptime(periodo_inicio, '%Y-%m-%d').date()
            pf = datetime.strptime(periodo_fin, '%Y-%m-%d').date()
        except ValueError:
            return Response({'detail': 'Formato de fecha inválido (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        if pf < pi:
            return Response({'periodo_fin': ['Debe ser posterior a periodo_inicio.']}, status=status.HTTP_400_BAD_REQUEST)

        fpago = None
        if fecha_pago:
            try:
                fpago = datetime.strptime(fecha_pago, '%Y-%m-%d').date()
            except ValueError:
                return Response({'detail': 'Formato de fecha inválido (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        empleados_qs = Empleado.objects.filter(activo=True)
        if getattr(user, "is_superuser", False):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)
            empleados_qs = empleados_qs.filter(empresa=empresa)
        if sucursal_id:
            from nucleo.models import Sucursal
            sucursal_qs = Sucursal.objects.filter(pk=sucursal_id)
            if not getattr(user, "is_superuser", False):
                sucursal_empresa = getattr(user, "empresa", None)
                if sucursal_empresa:
                    sucursal_qs = sucursal_qs.filter(empresa=sucursal_empresa)
            if not sucursal_qs.exists():
                return Response({'sucursal_id': ['La sucursal no existe o no pertenece a la empresa del usuario.']}, status=status.HTTP_400_BAD_REQUEST)
            empleados_qs = empleados_qs.filter(sucursal_id=sucursal_id)

        creadas = []
        for empleado in empleados_qs.select_related('sucursal', 'empresa', 'puesto').all():
            salario_base = empleado.puesto.salario_base if (empleado.puesto and empleado.puesto.salario_base) else None
            contrato_activo = empleado.contratos.filter(estado='activo').order_by('-fecha_inicio').first()
            if contrato_activo and contrato_activo.salario:
                salario_base = contrato_activo.salario

            nomina = Nomina.objects.create(
                empresa=empleado.empresa,
                sucursal=empleado.sucursal,
                empleado=empleado,
                periodo_inicio=pi,
                periodo_fin=pf,
                fecha_pago=fpago,
                estado='pendiente',
                salario_base=salario_base,
                dias_pagados=15,
                creado_por=request.user,
            )
            if salario_base:
                percepcion_monto = (salario_base / Decimal('30.0')) * Decimal('15')
                NominaDetalle.objects.create(
                    nomina=nomina,
                    codigo='PER001',
                    concepto='Salario base',
                    tipo='percepcion',
                    cantidad=1,
                    unidad='MXN',
                    monto=Decimal(percepcion_monto).quantize(Decimal('0.01')),
                )
            nomina._recalcular_totales()
            creadas.append(nomina.pk)

        return Response({
            'creadas': len(creadas),
            'ids': creadas,
        }, status=status.HTTP_201_CREATED)


class ProductividadViewSet(
    SoftDeleteDestroyMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    GenericViewSet
):
    queryset = Productividad.objects.all()
    serializer_class = ProductividadSerializer
    filter_backends = FILTER_BACKENDS
    filterset_fields = {
        'empleado': ['exact'],
        'departamento': ['exact'],
        'estado': ['exact'],
        'fecha': ['exact', 'gte', 'lte'],
        'meta_unidad': ['exact'],
    }
    search_fields = ['descripcion', 'empleado__nombre']
    ordering_fields = ['fecha', 'meta', 'resultado']
    ordering = ['-fecha']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related(
            "empresa", "departamento", "empleado", "meta_unidad", "creado_por"
        ).prefetch_related("detalles")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        return qs.filter(empresa=empresa)

    def perform_create(self, serializer):
        serializer.save(creado_por=self.request.user)


class DashboardRH(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        es_superuser = getattr(user, "is_superuser", False)
        empresa = None if es_superuser else getattr(user, "empresa", None)
        if not es_superuser and not empresa:
            return Response({'detail': 'No autorizado.'}, status=status.HTTP_403_FORBIDDEN)

        sucursal_id = request.query_params.get('sucursal_id')
        departamento_id = request.query_params.get('departamento_id')
        fecha_str = request.query_params.get('fecha')
        if fecha_str:
            try:
                hoy = datetime.strptime(fecha_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'detail': 'Formato de fecha inválido (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            hoy = date.today()
        inicio_mes = hoy.replace(day=1)
        prox_mes = (inicio_mes + timedelta(days=32)).replace(day=1)
        fin_mes = prox_mes - timedelta(days=1)
        inicio_semana = hoy - timedelta(days=hoy.weekday())
        fin_semana = inicio_semana + timedelta(days=6)

        empleados_qs = Empleado.objects.all()
        asistencias_qs = Asistencia.objects.all()
        vacaciones_qs = Vacaciones.objects.all()
        permisos_qs = PermisoAusencia.objects.all()
        incidencias_qs = Incidencia.objects.all()
        contratos_qs = Contrato.objects.all()
        nominas_qs = Nomina.objects.all()
        capacitaciones_qs = Capacitacion.objects.all()
        evaluaciones_qs = Evaluacion.objects.all()
        if not es_superuser:
            empleados_qs = empleados_qs.filter(empresa=empresa)
            asistencias_qs = asistencias_qs.filter(empleado__empresa=empresa)
            vacaciones_qs = vacaciones_qs.filter(empleado__empresa=empresa)
            permisos_qs = permisos_qs.filter(empleado__empresa=empresa)
            incidencias_qs = incidencias_qs.filter(empleado__empresa=empresa)
            contratos_qs = contratos_qs.filter(empleado__empresa=empresa)
            nominas_qs = nominas_qs.filter(empresa=empresa)
            capacitaciones_qs = capacitaciones_qs.filter(empleado__empresa=empresa)
            evaluaciones_qs = evaluaciones_qs.filter(empleado__empresa=empresa)
        if sucursal_id:
            empleados_qs = empleados_qs.filter(sucursal_id=sucursal_id)
            asistencias_qs = asistencias_qs.filter(empleado__sucursal_id=sucursal_id)
            vacaciones_qs = vacaciones_qs.filter(empleado__sucursal_id=sucursal_id)
            permisos_qs = permisos_qs.filter(empleado__sucursal_id=sucursal_id)
            incidencias_qs = incidencias_qs.filter(empleado__sucursal_id=sucursal_id)
            contratos_qs = contratos_qs.filter(empleado__sucursal_id=sucursal_id)
            nominas_qs = nominas_qs.filter(sucursal_id=sucursal_id)
            capacitaciones_qs = capacitaciones_qs.filter(empleado__sucursal_id=sucursal_id)
            evaluaciones_qs = evaluaciones_qs.filter(empleado__sucursal_id=sucursal_id)
        if departamento_id:
            empleados_qs = empleados_qs.filter(departamento_id=departamento_id)
            asistencias_qs = asistencias_qs.filter(empleado__departamento_id=departamento_id)
            vacaciones_qs = vacaciones_qs.filter(empleado__departamento_id=departamento_id)
            permisos_qs = permisos_qs.filter(empleado__departamento_id=departamento_id)
            incidencias_qs = incidencias_qs.filter(empleado__departamento_id=departamento_id)
            contratos_qs = contratos_qs.filter(empleado__departamento_id=departamento_id)
            capacitaciones_qs = capacitaciones_qs.filter(empleado__departamento_id=departamento_id)
            evaluaciones_qs = evaluaciones_qs.filter(empleado__departamento_id=departamento_id)

        empleados_activos = empleados_qs.filter(activo=True).count()
        altas_mes = empleados_qs.filter(fecha_ingreso__gte=inicio_mes, fecha_ingreso__lte=fin_mes).count()
        bajas_mes = empleados_qs.filter(activo=False, fecha_baja__gte=inicio_mes, fecha_baja__lte=fin_mes).count()

        asistencias_hoy_qs = asistencias_qs.filter(fecha=hoy)
        asistencias_hoy = {
            'total_registradas': asistencias_hoy_qs.count(),
            'puntual': asistencias_hoy_qs.filter(estado='puntual').count(),
            'retardo': asistencias_hoy_qs.filter(estado='retardo').count(),
            'falta': asistencias_hoy_qs.filter(estado='falta').count(),
            'salida_registrada': asistencias_hoy_qs.filter(hora_salida__isnull=False).count(),
        }
        asistencias_hoy['sin_registrar'] = max(0, empleados_activos - asistencias_hoy['total_registradas'])

        vacaciones_pendientes = vacaciones_qs.filter(estado='pendiente').count()
        permisos_pendientes = permisos_qs.filter(estado='pendiente').count()

        horas_extra_semana = asistencias_qs.filter(
            fecha__gte=inicio_semana, fecha__lte=fin_semana
        ).aggregate(total=Coalesce(Sum('horas_extra'), Value(0, output_field=DecimalField())))['total'] or Decimal('0')

        incidencias_abiertas = incidencias_qs.filter(estado='abierto').count()

        nomina_periodo_actual_qs = nominas_qs.filter(
            periodo_inicio__lte=hoy,
            periodo_fin__gte=hoy,
            estado__in=['pendiente', 'pagada'],
        )
        nominas_pendientes_pagar = nominas_qs.filter(estado='pendiente').count()
        neto_total_periodo_actual = nomina_periodo_actual_qs.aggregate(
            total=Coalesce(Sum('neto'), Value(0, output_field=DecimalField()))
        )['total'] or Decimal('0')

        # Departamento y Sucursal usan pk propias (id_departamento / id_sucursal):
        # no existe un campo 'id' que resolver. Se alias-ea para conservar
        # intactas las claves 'departamento__id' y 'sucursal__id' de la respuesta.
        distribucion_por_departamento = list(
            empleados_qs.filter(activo=True, departamento__isnull=False)
            .values('departamento__nombre', departamento__id=F('departamento__id_departamento'))
            .annotate(total=Count('id'))
            .order_by('-total')
        )
        distribucion_por_sucursal = list(
            empleados_qs.filter(activo=True, sucursal__isnull=False)
            .values('sucursal__nombre', sucursal__id=F('sucursal__id_sucursal'))
            .annotate(total=Count('id'))
            .order_by('-total')
        )

        if empleados_activos > 0:
            tasa_rotacion = round(((altas_mes + bajas_mes) / empleados_activos) * 100, 2)
        else:
            tasa_rotacion = 0.0
        rotacion_mes = {'altas': altas_mes, 'bajas': bajas_mes, 'tasa_porcentual': tasa_rotacion}

        capacitaciones_en_curso = capacitaciones_qs.filter(estado='en_curso').count()
        evaluaciones_pendientes = evaluaciones_qs.filter(estado='pendiente').count()

        return Response({
            'empleados_activos': empleados_activos,
            'altas_mes': altas_mes,
            'bajas_mes': bajas_mes,
            'asistencias_hoy': asistencias_hoy,
            'vacaciones_pendientes': vacaciones_pendientes,
            'permisos_pendientes': permisos_pendientes,
            'horas_extra_semana': horas_extra_semana,
            'incidencias_abiertas': incidencias_abiertas,
            'nominas': {
                'pendientes_pagar': nominas_pendientes_pagar,
                'neto_total_periodo_actual': neto_total_periodo_actual,
            },
            'distribucion_por_departamento': distribucion_por_departamento,
            'distribucion_por_sucursal': distribucion_por_sucursal,
            'rotacion_mes': rotacion_mes,
            'capacitaciones_en_curso': capacitaciones_en_curso,
            'evaluaciones_pendientes': evaluaciones_pendientes,
        })
