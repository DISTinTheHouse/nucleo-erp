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
from django.utils import timezone
from decimal import Decimal


class EmpresaScopedSerializerMixin:
    """Aislamiento multi-tenant en ESCRITURA (POST/PUT/PATCH) para HR."""

    def _empresa_usuario(self):
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

    def validate_meta_unidad(self, meta_unidad):
        return meta_unidad


class PuestoSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Puesto
        fields = '__all__'

    def validate_area(self, area):
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

    def validate_nss(self, value):
        if value and len(value) != 11:
            raise serializers.ValidationError("El NSS debe tener 11 dígitos.")
        if value and not value.isdigit():
            raise serializers.ValidationError("El NSS debe contener solo dígitos.")
        return value

    def validate_clabe(self, value):
        if value and len(value) != 18:
            raise serializers.ValidationError("La CLABE debe tener 18 dígitos.")
        if value and not value.isdigit():
            raise serializers.ValidationError("La CLABE debe contener solo dígitos.")
        return value

    def validate_curp(self, value):
        if value and len(value) != 18:
            raise serializers.ValidationError("La CURP debe tener 18 caracteres.")
        return value

    def validate_rfc(self, value):
        if value and len(value) not in (12, 13):
            raise serializers.ValidationError("El RFC debe tener 12 o 13 caracteres.")
        return value


class AreaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Area
        fields = '__all__'

    def validate_responsable(self, responsable):
        return self.validate_empleado(responsable)


class ContratoSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Contrato
        fields = '__all__'

    def validate(self, data):
        empleado = data.get('empleado') or getattr(self.instance, 'empleado', None)
        estado = data.get('estado') or getattr(self.instance, 'estado', None)
        if empleado and estado == 'activo':
            qs = Contrato.objects.filter(empleado=empleado, estado='activo')
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({'estado': 'Este empleado ya tiene un contrato activo.'})
        fecha_inicio = data.get('fecha_inicio')
        fecha_fin = data.get('fecha_fin')
        if fecha_inicio and fecha_fin and fecha_fin < fecha_inicio:
            raise serializers.ValidationError({'fecha_fin': 'La fecha de fin no puede ser anterior a la de inicio.'})
        return data


class TurnoSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Turno
        fields = '__all__'

    def validate(self, data):
        hora_entrada = data.get('hora_entrada') or getattr(self.instance, 'hora_entrada', None)
        hora_salida = data.get('hora_salida') or getattr(self.instance, 'hora_salida', None)
        if hora_entrada and hora_salida:
            hoy = timezone.now().date()
            from datetime import datetime as _dt
            e = _dt.combine(hoy, hora_entrada)
            s = _dt.combine(hoy, hora_salida)
            if s <= e:
                raise serializers.ValidationError({'hora_salida': 'La hora de salida debe ser posterior a la de entrada.'})
        return data


class CalendarioSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Calendario
        fields = '__all__'

    def validate_turno(self, turno):
        if turno is None:
            return turno
        self._validar_empresa_id(
            turno.empresa_id,
            "El turno del calendario no pertenece a la empresa del usuario.",
        )
        return turno


class AsistenciaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Asistencia
        fields = '__all__'

    def validate(self, data):
        hora_salida = data.get('hora_salida')
        hora_entrada = data.get('hora_entrada') or (self.instance.hora_entrada if self.instance else None)
        if hora_salida and hora_entrada and hora_salida < hora_entrada:
            raise serializers.ValidationError({'hora_salida': 'La hora de salida no puede ser anterior a la de entrada.'})
        return data


class ControlHorasSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = ControlHoras
        fields = '__all__'

    def validate_asistencia(self, asistencia):
        if asistencia is None:
            return asistencia
        self._validar_empresa_id(
            asistencia.empleado.empresa_id,
            "La asistencia no pertenece a la empresa del usuario.",
        )
        return asistencia

    def validate_op(self, op):
        if op is None:
            return op
        self._validar_empresa_id(
            op.empresa_id,
            "La orden de producción no pertenece a la empresa del usuario.",
        )
        return op

    def validate(self, data):
        hora_inicio = data.get('hora_inicio') or (self.instance.hora_inicio if self.instance else None)
        hora_fin = data.get('hora_fin')
        if hora_inicio and hora_fin and hora_fin < hora_inicio:
            raise serializers.ValidationError({'hora_fin': 'La hora de fin no puede ser anterior a la de inicio.'})
        return data


class VacacionesSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Vacaciones
        fields = '__all__'
        read_only_fields = ('fecha_solicitud', 'autorizado_por', 'rechazado_por', 'fecha_aprobacion', 'fecha_rechazo', 'solicitado_por')

    def validate(self, data):
        fecha_inicio = data.get('fecha_inicio')
        fecha_fin = data.get('fecha_fin')
        if fecha_inicio and fecha_fin and fecha_fin < fecha_inicio:
            raise serializers.ValidationError({'fecha_fin': 'La fecha de fin no puede ser anterior a la de inicio.'})
        return data


class PermisoAusenciaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = PermisoAusencia
        fields = '__all__'
        read_only_fields = ('fecha_solicitud', 'autorizado_por', 'rechazado_por', 'fecha_aprobacion', 'fecha_rechazo', 'solicitado_por')

    def validate(self, data):
        fecha_inicio = data.get('fecha_inicio')
        fecha_fin = data.get('fecha_fin')
        if fecha_inicio and fecha_fin and fecha_fin < fecha_inicio:
            raise serializers.ValidationError({'fecha_fin': 'La fecha de fin no puede ser anterior a la de inicio.'})
        return data


class IncidenciaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Incidencia
        fields = '__all__'
        read_only_fields = ('fecha_reporte', 'reportado_por')


class EvaluacionSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Evaluacion
        fields = '__all__'

    def validate_evaluador(self, evaluador):
        return self.validate_empleado(evaluador)


class CapacitacionSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Capacitacion
        fields = '__all__'


class NominaDetalleSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = NominaDetalle
        fields = '__all__'

    def validate_nomina(self, nomina):
        if nomina is None:
            return nomina
        self._validar_empresa_id(
            nomina.empresa_id,
            "La nómina no pertenece a la empresa del usuario.",
        )
        return nomina

    def validate(self, data):
        monto = data.get('monto')
        if monto is not None and monto < 0:
            raise serializers.ValidationError({'monto': 'El monto no puede ser negativo.'})
        return data


class NominaSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    detalles = NominaDetalleSerializer(many=True, required=False)

    class Meta:
        model = Nomina
        fields = '__all__'
        read_only_fields = ('fecha_generacion', 'total_percepciones', 'total_deducciones', 'neto', 'creado_por')

    def validate(self, data):
        periodo_inicio = data.get('periodo_inicio')
        periodo_fin = data.get('periodo_fin')
        if periodo_inicio and periodo_fin and periodo_fin < periodo_inicio:
            raise serializers.ValidationError({'periodo_fin': 'El periodo fin no puede ser anterior al inicio.'})
        return data

    def create(self, validated_data):
        detalles_data = validated_data.pop('detalles', [])
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user and not validated_data.get('creado_por'):
            validated_data['creado_por'] = user
        nomina = Nomina.objects.create(**validated_data)
        for detalle_data in detalles_data:
            NominaDetalle.objects.create(nomina=nomina, **detalle_data)
        nomina._recalcular_totales()
        return nomina

    def update(self, instance, validated_data):
        detalles_data = validated_data.pop('detalles', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if detalles_data is not None:
            instance.detalles.all().delete()
            for detalle_data in detalles_data:
                NominaDetalle.objects.create(nomina=instance, **detalle_data)
            instance._recalcular_totales()
        return instance


class ProductividadSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Productividad
        fields = '__all__'


class ProductividadDetalleSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = ProductividadDetalle
        fields = '__all__'

    def validate_productividad(self, productividad):
        if productividad is None:
            return productividad
        self._validar_empresa_id(
            productividad.empresa_id,
            "La productividad no pertenece a la empresa del usuario.",
        )
        return productividad
