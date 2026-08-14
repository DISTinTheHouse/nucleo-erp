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


class EmpresaScopedSerializerMixin:
    """Aislamiento multi-tenant en ESCRITURA (POST/PUT/PATCH) para HR.

    ``get_queryset`` de los ViewSets sólo acota LECTURAS. Como todos estos
    serializers usan ``fields = '__all__'``, cada FK se resuelve contra
    ``Modelo.objects.all()``: sin esta validación un usuario podría crear —o
    mover— un registro apuntando a la empresa, el empleado o el turno de otra
    empresa. Misma convención empresa-only que ``PedidoDetalleSerializer``
    (ventas) y ``SerieFolioSerializer`` (nucleo): superuser puede todo; un
    usuario sin empresa asignada no puede escribir; el resto sólo su empresa.
    """

    def _empresa_usuario(self):
        """Devuelve ``(empresa, es_superuser)`` del usuario de la petición."""
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if getattr(user, "is_superuser", False):
            return None, True
        return getattr(user, "empresa", None), False

    def _validar_empresa_id(self, empresa_id, mensaje):
        empresa, es_superuser = self._empresa_usuario()
        if es_superuser:
            return
        if empresa is None or empresa_id != empresa.pk:
            raise serializers.ValidationError(mensaje)

    # Validadores reutilizados por los distintos serializers según la FK que
    # cada modelo use para llegar a la empresa.

    def validate_empresa(self, empresa):
        self._validar_empresa_id(
            getattr(empresa, "pk", None),
            "La empresa no corresponde a la empresa del usuario.",
        )
        return empresa

    def validate_sucursal(self, sucursal):
        if sucursal is None:
            return sucursal
        self._validar_empresa_id(
            sucursal.empresa_id,
            "La sucursal no pertenece a la empresa del usuario.",
        )
        return sucursal

    def validate_departamento(self, departamento):
        if departamento is None:
            return departamento
        self._validar_empresa_id(
            departamento.empresa_id,
            "El departamento no pertenece a la empresa del usuario.",
        )
        return departamento

    def validate_empleado(self, empleado):
        if empleado is None:
            return empleado
        self._validar_empresa_id(
            empleado.empresa_id,
            "El empleado no pertenece a la empresa del usuario.",
        )
        return empleado

    def validate_puesto(self, puesto):
        if puesto is None:
            return puesto
        self._validar_empresa_id(
            puesto.empresa_id,
            "El puesto no pertenece a la empresa del usuario.",
        )
        return puesto

    def validate_turno(self, turno):
        if turno is None:
            return turno
        self._validar_empresa_id(
            turno.empresa_id,
            "El turno no pertenece a la empresa del usuario.",
        )
        return turno


class PuestoSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Puesto
        fields = '__all__'

    def validate_area(self, area):
        # ``Area`` hereda la empresa por ``departamento``.
        if area is None:
            return area
        self._validar_empresa_id(
            area.departamento.empresa_id,
            "El área no pertenece a la empresa del usuario.",
        )
        return area

class EmpleadoSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Empleado
        fields = '__all__'

class AreaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Area
        fields = '__all__'

    def validate_responsable(self, responsable):
        # ``responsable`` es un ``Empleado``: se valida por su empresa.
        return self.validate_empleado(responsable)

class ContratoSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Contrato
        fields = '__all__'

class TurnoSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Turno
        fields = '__all__'

class CalendarioSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Calendario
        fields = '__all__'

class AsistenciaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Asistencia
        fields = '__all__'

class ControlHorasSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = ControlHoras
        fields = '__all__'

    def validate_asistencia(self, asistencia):
        # ``Asistencia`` hereda la empresa por ``empleado``.
        if asistencia is None:
            return asistencia
        self._validar_empresa_id(
            asistencia.empleado.empresa_id,
            "La asistencia no pertenece a la empresa del usuario.",
        )
        return asistencia

    def validate_op(self, op):
        # ``op`` es una ``produccion.OrdenProduccion``, que tiene FK ``empresa``
        # propia: sin esto se podían imputar horas contra la OP de otra empresa.
        if op is None:
            return op
        self._validar_empresa_id(
            op.empresa_id,
            "La orden de producción no pertenece a la empresa del usuario.",
        )
        return op

class VacacionesSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Vacaciones
        fields = '__all__'

class PermisoAusenciaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = PermisoAusencia
        fields = '__all__'

class IncidenciaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Incidencia
        fields = '__all__'

class EvaluacionSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Evaluacion
        fields = '__all__'

    def validate_evaluador(self, evaluador):
        # ``evaluador`` es un ``Empleado``: se valida por su empresa.
        return self.validate_empleado(evaluador)

class CapacitacionSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Capacitacion
        fields = '__all__'

class NominaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Nomina
        fields = '__all__'

class NominaDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = NominaDetalle
        fields = '__all__'

class ProductividadSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Productividad
        fields = '__all__'

class ProductividadDetalleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductividadDetalle
        fields = '__all__'
