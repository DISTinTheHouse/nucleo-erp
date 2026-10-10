from contextlib import contextmanager

from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework.settings import api_settings
from rest_framework.validators import UniqueTogetherValidator
from hr.models import (
    fecha_local,
    MENSAJE_CONTRATO_VIGENTE_DUPLICADO,
    MENSAJE_CONTRATO_VIGENTE_EMPLEADO_INACTIVO,
    MENSAJE_NOMINA_PERIODO_DUPLICADO,
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

    def validate_autorizado_por(self, autorizado_por):
        # ``autorizado_por`` es un ``usuarios.Usuario``, que llega a la empresa por
        # su FK directa ``empresa`` (misma ruta que usa el pipeline de picking en
        # ``wms.services.picking_pipeline.context`` para validar al operador).
        if autorizado_por is None:
            return autorizado_por
        self._validar_empresa_id(
            autorizado_por.empresa_id,
            "El usuario autorizador no pertenece a la empresa del usuario.",
        )
        return autorizado_por

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
        # Campo de auditoria: lo fija el servidor en ContratoViewSet.perform_create.
        # Sin esto un PUT/PATCH podia apuntarlo al usuario de otra empresa. Misma
        # convencion que 'creado_por' en NominaSerializer y 'reportado_por' en
        # IncidenciaSerializer; sigue presente en la respuesta.
        read_only_fields = ('creado_por',)

    def validate(self, data):
        if Contrato.vigente_con_empleado_inactivo(
            self._final(data, 'empleado'),
            activo=self._final(data, 'activo'),
            estado=self._final(data, 'estado'),
        ):
            raise serializers.ValidationError({'empleado': MENSAJE_CONTRATO_VIGENTE_EMPLEADO_INACTIVO})
        if self._chocaria_con_otro_vigente(data):
            raise serializers.ValidationError({'estado': MENSAJE_CONTRATO_VIGENTE_DUPLICADO})
        fecha_inicio = data.get('fecha_inicio')
        fecha_fin = data.get('fecha_fin')
        if fecha_inicio and fecha_fin and fecha_fin < fecha_inicio:
            raise serializers.ValidationError({'fecha_fin': 'La fecha de fin no puede ser anterior a la de inicio.'})
        return data

    def create(self, validated_data):
        with self._choque_de_vigente_como_400(validated_data):
            return super().create(validated_data)

    def update(self, instance, validated_data):
        with self._choque_de_vigente_como_400(validated_data):
            return super().update(instance, validated_data)

    def _final(self, data, campo):
        """Valor con el que QUEDARÁ ``campo`` tras guardar.

        Lo que no viene en la petición conserva el valor de la fila en una
        edición y, en un alta, toma el default del modelo --el mismo con el que
        se guardará--. Antes un alta sin ``estado`` resolvía a ``None``, se
        saltaba la revisión y nacía 'activo' por el default.
        """
        if campo in data:
            return data[campo]
        if self.instance is not None:
            return getattr(self.instance, campo)
        return Contrato._meta.get_field(campo).get_default()

    def _chocaria_con_otro_vigente(self, data):
        """Aplica ``uq_contrato_vigente_por_empleado`` con los valores finales.

        ``empleado`` ya pasó por ``validate_empleado`` (o viene de una fila que
        ``get_queryset`` dejó ver), así que la consulta no cruza empresas.
        """
        return Contrato.hay_otro_vigente(
            getattr(self._final(data, 'empleado'), 'pk', None),
            activo=self._final(data, 'activo'),
            estado=self._final(data, 'estado'),
            excluir_pk=getattr(self.instance, 'pk', None),
        )

    @contextmanager
    def _choque_de_vigente_como_400(self, validated_data):
        """Traduce la violación de la constraint al mismo 400 de ``validate``.

        Pasa si otra petición deja vigente un contrato del mismo empleado entre
        ``validate`` y el INSERT/UPDATE. No se lee el texto del
        ``IntegrityError`` (PostgreSQL nombra la constraint, SQLite no): se
        re-consulta, como en ``produccion.services.common``, y cualquier otro
        ``IntegrityError`` se re-lanza. El savepoint deja usable la transacción
        para esa re-consulta.
        """
        try:
            with transaction.atomic():
                yield
        except IntegrityError as exc:
            if not self._chocaria_con_otro_vigente(validated_data):
                raise
            # Fuera de ``validate`` DRF no envuelve el mensaje en una lista; se
            # hace aquí para que el cuerpo sea idéntico al de la ruta normal.
            raise serializers.ValidationError(
                {'estado': [MENSAJE_CONTRATO_VIGENTE_DUPLICADO]}
            ) from exc


class TurnoSerializer(EmpresaScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Turno
        fields = '__all__'

    def validate(self, data):
        hora_entrada = data.get('hora_entrada') or getattr(self.instance, 'hora_entrada', None)
        hora_salida = data.get('hora_salida') or getattr(self.instance, 'hora_salida', None)
        if hora_entrada and hora_salida:
            hoy = timezone.localdate()
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
        # Los calcula ``Asistencia._calcular_estado_y_horas`` en cada guardado.
        # ``estado`` sigue escribible: solo ``justificada`` se conserva, cualquier
        # otro valor se reemplaza por el derivado.
        read_only_fields = ('minutos_retardo', 'minutos_tolerancia', 'horas_normales', 'horas_extra')

    def _final(self, data, campo):
        """Valor con el que quedará ``campo``: el recibido o, en una edición, el guardado."""
        if campo in data:
            return data[campo]
        return getattr(self.instance, campo, None)

    def create(self, validated_data):
        with self._choque_de_unicidad_como_400(validated_data):
            return super().create(validated_data)

    def update(self, instance, validated_data):
        with self._choque_de_unicidad_como_400(validated_data):
            return super().update(instance, validated_data)

    @contextmanager
    def _choque_de_unicidad_como_400(self, validated_data):
        """Traduce la violación de ``unique_asistencia_empleado_fecha`` al 400 del validador.

        Pasa si otra petición (p. ej. ``registrar_entrada``) escribe la fila de
        ``(empleado, fecha)`` entre el validador de unicidad de DRF y el
        INSERT/UPDATE. Misma técnica que ``ContratoSerializer``: savepoint propio,
        re-consulta en vez de leer el texto del ``IntegrityError`` (PostgreSQL
        nombra la constraint, SQLite no) y cualquier otro ``IntegrityError`` se
        re-lanza.
        """
        empleado = self._final(validated_data, 'empleado')
        fecha = self._final(validated_data, 'fecha')
        try:
            with transaction.atomic():
                yield
        except IntegrityError as exc:
            rivales = Asistencia.objects.filter(empleado=empleado, fecha=fecha)
            if self.instance is not None:
                rivales = rivales.exclude(pk=self.instance.pk)
            if not rivales.exists():
                raise
            raise serializers.ValidationError(
                {api_settings.NON_FIELD_ERRORS_KEY: [self._mensaje_de_unicidad()]}, code='unique',
            ) from exc

    def _mensaje_de_unicidad(self):
        """El mismo texto que el ``UniqueTogetherValidator`` de ``(empleado, fecha)``."""
        for validador in self.validators:
            if isinstance(validador, UniqueTogetherValidator) and set(validador.fields) == {'empleado', 'fecha'}:
                return validador.message.format(field_names=', '.join(validador.fields))
        raise RuntimeError('AsistenciaSerializer perdió el validador de unicidad de (empleado, fecha).')

    def validate(self, data):
        fecha = self._final(data, 'fecha')
        hora_entrada = self._final(data, 'hora_entrada')
        hora_salida = self._final(data, 'hora_salida')

        # Una hora de otro día desfasaba el retardo y las horas; una salida días
        # después de la entrada desbordaba ``Decimal(4,2)`` (500 en Postgres).
        for campo, valor, nombre in (
            ('hora_entrada', hora_entrada, 'entrada'),
            ('hora_salida', hora_salida, 'salida'),
        ):
            if valor and fecha and fecha_local(valor) != fecha:
                raise serializers.ValidationError(
                    {campo: f'La hora de {nombre} debe corresponder a la fecha de la asistencia.'}
                )
        if hora_salida and not hora_entrada:
            # El error va en el campo que mandó el cliente: un PATCH que solo
            # quita la entrada sobre una salida guardada no tocó ``hora_salida``.
            if 'hora_entrada' in data and 'hora_salida' not in data:
                raise serializers.ValidationError(
                    {'hora_entrada': 'No se puede quitar la hora de entrada mientras haya una hora de salida.'}
                )
            raise serializers.ValidationError(
                {'hora_salida': 'No se puede registrar la salida sin una hora de entrada.'}
            )
        # ``<=``: un turno de duración cero tampoco es válido, igual que en
        # ``registrar_salida`` y en ``Asistencia.clean``.
        if hora_salida and hora_entrada and hora_salida <= hora_entrada:
            raise serializers.ValidationError({'hora_salida': 'La hora de salida debe ser posterior a la de entrada.'})
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
        # ``estado`` solo cambia via las acciones aprobar/rechazar (que escriben
        # directo sobre el modelo, no por este serializer) — nunca por PUT/PATCH.
        read_only_fields = ('estado', 'fecha_solicitud', 'autorizado_por', 'rechazado_por', 'fecha_aprobacion', 'fecha_rechazo', 'solicitado_por')

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
        # Mismo motivo que en VacacionesSerializer: ``estado`` solo cambia via
        # aprobar/rechazar.
        read_only_fields = ('estado', 'fecha_solicitud', 'autorizado_por', 'rechazado_por', 'fecha_aprobacion', 'fecha_rechazo', 'solicitado_por')

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
        # Se escribe siempre desde la nomina padre en NominaSerializer.create/update.
        # Como entrada obligatoria chocaba con ese nomina=nomina (TypeError por
        # argumento duplicado); sigue visible en la respuesta.
        read_only_fields = ('nomina',)

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
        # DRF deriva un ``UniqueTogetherValidator`` de
        # ``uq_nomina_empleado_periodo_vigente``: reporta en ``non_field_errors``,
        # vuelve requerido ``estado`` (campo de su condición) y en un PATCH sin
        # ``estado`` revienta con ``KeyError`` (500). Se desactiva; de la unicidad
        # se encarga ``_validar_periodo_unico``. Ojo: vaciar ``validators`` apaga
        # TODOS los derivados; hoy ``Nomina`` no tiene otra constraint única.
        validators = []

    def validate_detalles(self, detalles):
        """En un PATCH, cada línea se valida completa, igual que en el alta.

        En modo parcial DRF salta los campos requeridos de TODO el árbol (lo
        decide ``self.root.partial``), también los de las líneas anidadas: una
        línea sin ``monto`` llegaba al INSERT (500) y una sin ``tipo`` o
        ``concepto`` se guardaba con ``''``. Pero ``detalles`` reemplaza todas las
        líneas, así que lo que llega debe ser una línea completa. Se revalida con
        un serializer propio, que es su propia raíz y no es parcial.
        """
        if not self.partial:
            return detalles
        completas = NominaDetalleSerializer(
            data=self.initial_data.get('detalles'), many=True, context=self.context,
        )
        if not completas.is_valid():
            raise serializers.ValidationError(completas.errors)
        return completas.validated_data

    def validate(self, data):
        periodo_inicio = self._final(data, 'periodo_inicio')
        periodo_fin = self._final(data, 'periodo_fin')
        # Con los valores finales: un PATCH que manda una sola fecha se compara
        # contra la guardada.
        if periodo_inicio and periodo_fin and periodo_fin < periodo_inicio:
            raise serializers.ValidationError({'periodo_fin': 'El periodo fin no puede ser anterior al inicio.'})
        self._validar_coherencia(data)
        self._validar_periodo_unico(data)
        return data

    def _validar_coherencia(self, data):
        """Empresa, sucursal y empleado de la misma empresa, y la sucursal es la
        del empleado. Con los valores finales y también para el superusuario."""
        empresa = self._final(data, 'empresa')
        sucursal = self._final(data, 'sucursal')
        empleado = self._final(data, 'empleado')
        errores = {}
        if empresa and sucursal and sucursal.empresa_id != empresa.pk:
            errores['sucursal'] = 'La sucursal no pertenece a la empresa de la nómina.'
        if empresa and empleado and empleado.empresa_id != empresa.pk:
            errores['empleado'] = 'El empleado no pertenece a la empresa de la nómina.'
        if not errores and sucursal and empleado and empleado.sucursal_id != sucursal.pk:
            errores['sucursal'] = 'La sucursal no es la del empleado.'
        if errores:
            raise serializers.ValidationError(errores)

    def _final(self, data, campo):
        """Valor con el que QUEDARÁ ``campo`` tras guardar.

        Lo que no viene en la petición conserva el valor de la fila en una
        edición y, en un alta, toma el default del modelo --el mismo con el que
        se guardará--.
        """
        if campo in data:
            return data[campo]
        if self.instance is not None:
            return getattr(self.instance, campo)
        return Nomina._meta.get_field(campo).get_default()

    def _chocaria_con_otra_vigente(self, data):
        """Aplica ``uq_nomina_empleado_periodo_vigente`` con los valores finales.

        ``empleado`` ya pasó por ``validate_empleado`` (o viene de una fila que
        ``get_queryset`` dejó ver), así que la consulta no cruza empresas.
        """
        return Nomina.hay_otra_vigente(
            getattr(self._final(data, 'empleado'), 'pk', None),
            self._final(data, 'periodo_inicio'),
            self._final(data, 'periodo_fin'),
            estado=self._final(data, 'estado'),
            excluir_pk=getattr(self.instance, 'pk', None),
        )

    def _validar_periodo_unico(self, data):
        """Devuelve un 400 por campo donde la constraint daría un 500.

        No se usa ``UniqueTogetherValidator`` (ver ``Meta.validators``): éste
        compara contra los valores finales, así que un PATCH que sólo mueve una
        fecha, cambia de empleado o reactiva una cancelada también se revisa.
        """
        if self._chocaria_con_otra_vigente(data):
            raise serializers.ValidationError({'empleado': MENSAJE_NOMINA_PERIODO_DUPLICADO})

    @contextmanager
    def _atomico_con_choque_de_periodo_como_400(self, validated_data):
        """Encabezado y líneas se guardan juntos o no se guarda nada.

        Y si otra petición crea la nómina vigente del mismo empleado y periodo
        entre ``validate`` y el INSERT/UPDATE, la violación de la constraint se
        traduce al mismo 400 de ``_validar_periodo_unico``. Misma técnica que
        ``ContratoSerializer``: se re-consulta en vez de leer el texto del
        ``IntegrityError`` y cualquier otro se re-lanza.
        """
        try:
            with transaction.atomic():
                yield
        except IntegrityError as exc:
            if not self._chocaria_con_otra_vigente(validated_data):
                raise
            # Fuera de ``validate`` DRF no envuelve el mensaje en una lista; se
            # hace aquí para que el cuerpo sea idéntico al de la ruta normal.
            raise serializers.ValidationError(
                {'empleado': [MENSAJE_NOMINA_PERIODO_DUPLICADO]}
            ) from exc

    def create(self, validated_data):
        with self._atomico_con_choque_de_periodo_como_400(validated_data):
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
        with self._atomico_con_choque_de_periodo_como_400(validated_data):
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
        # Campo de auditoria: lo fija el servidor en ProductividadViewSet.perform_create.
        # Sin esto un PUT/PATCH podia apuntarlo al usuario de otra empresa. Misma
        # convencion que 'creado_por' en NominaSerializer; sigue en la respuesta.
        read_only_fields = ('creado_por',)


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
