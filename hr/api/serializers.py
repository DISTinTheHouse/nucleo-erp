from rest_framework import serializers
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
    ProductividadDetalle
)

class PuestoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Puesto
        fields = '__all__'

class EmpleadoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Empleado
        fields = '__all__'

class AreaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Area
        fields = '__all__'

class ContratoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contrato
        fields = '__all__'

class TurnoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Turno
        fields = '__all__'

class CalendarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Calendario
        fields = '__all__'

class AsistenciaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asistencia
        fields = '__all__'

class ControlHorasSerializer(serializers.ModelSerializer):
    class Meta:
        model = ControlHoras
        fields = '__all__'

class VacacionesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vacaciones
        fields = '__all__'

class PermisoAusenciaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PermisoAusencia
        fields = '__all__'

class IncidenciaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Incidencia
        fields = '__all__'

class EvaluacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Evaluacion
        fields = '__all__'

class CapacitacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Capacitacion
        fields = '__all__'

class NominaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Nomina
        fields = '__all__'

class NominaDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = NominaDetalle
        fields = '__all__'

class ProductividadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Productividad
        fields = '__all__'
        
class ProductividadDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductividadDetalle
        fields = '__all__'