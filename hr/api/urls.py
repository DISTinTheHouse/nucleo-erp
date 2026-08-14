from django.urls import path, include
from rest_framework.routers import DefaultRouter
from hr.api.views import (
    PuestoViewSet,
    EmpleadoViewSet,
    AreaViewSet,
    ContratoViewSet,
    TurnoViewSet,
    CalendarioViewSet,
    AsistenciaViewSet,
    ControlHorasViewSet,
    VacacionesViewSet,
    PermisoAusenciaViewSet,
    IncidenciaViewSet,
    EvaluacionViewSet,
    CapacitacionViewSet,
    NominaViewSet,
    ProductividadViewSet
)

router = DefaultRouter()
router.register(r'puestos', PuestoViewSet, basename='puestos')
router.register(r'empleados', EmpleadoViewSet, basename='empleados')
router.register(r'areas', AreaViewSet, basename='areas')
router.register(r'contratos', ContratoViewSet, basename='contratos')
router.register(r'turnos', TurnoViewSet, basename='turnos')
router.register(r'calendarios', CalendarioViewSet, basename='calendarios')
router.register(r'asistencias', AsistenciaViewSet, basename='asistencias')
router.register(r'control-horas', ControlHorasViewSet, basename='control-horas')
router.register(r'vacaciones', VacacionesViewSet, basename='vacaciones')
router.register(r'permisos-ausencias', PermisoAusenciaViewSet, basename='permisos-ausencias')
router.register(r'incidencias', IncidenciaViewSet, basename='incidencias')
router.register(r'evaluaciones', EvaluacionViewSet, basename='evaluaciones')
router.register(r'capacitaciones', CapacitacionViewSet, basename='capacitaciones')
router.register(r'nominas', NominaViewSet, basename='nominas')
router.register(r'productividad', ProductividadViewSet, basename='productividad')

urlpatterns = [
    path('', include(router.urls)),
]