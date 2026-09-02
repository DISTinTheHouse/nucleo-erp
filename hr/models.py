from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone
from nucleo.models import StatusLifecycleModel
from datetime import date, datetime, timedelta
from decimal import Decimal


def _con_zona_horaria(valor):
    """Normaliza un datetime a la zona horaria activa.

    Con ``USE_TZ`` habilitado, ``hora_entrada``/``hora_salida`` pueden llegar
    aware (DRF, ``timezone.now()``) o naive (``datetime.strptime`` en los
    endpoints del checador), y ``datetime.combine`` del turno siempre es naive.
    Restar uno de otro lanzaba ``TypeError``. Se usa la misma zona con la que
    Django convertiría un valor naive al guardarlo, así que el instante
    almacenado no cambia.
    """
    if valor is None or not settings.USE_TZ or timezone.is_aware(valor):
        return valor
    return timezone.make_aware(valor, timezone.get_current_timezone())


class Puesto(StatusLifecycleModel):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.PROTECT, related_name='puestos')
    nombre = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True, null=True)
    salario_base = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    area = models.ForeignKey('hr.Area', on_delete=models.PROTECT, related_name='puestos', blank=True, null=True)

    class Meta:
        db_table = "puestos"
        verbose_name = "Puesto"
        verbose_name_plural = "Puestos"

    def __str__(self):
        return self.nombre


class Empleado(StatusLifecycleModel):
    SEXO_CHOICES = [
        ('M', 'Masculino'),
        ('F', 'Femenino'),
        ('NB', 'No binario'),
    ]
    ESTADO_CIVIL_CHOICES = [
        ('soltero', 'Soltero(a)'),
        ('casado', 'Casado(a)'),
        ('divorciado', 'Divorciado(a)'),
        ('viudo', 'Viudo(a)'),
        ('union_libre', 'Unión libre'),
    ]

    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.PROTECT, related_name='empleados')
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.PROTECT, related_name='empleados')
    departamento = models.ForeignKey('nucleo.Departamento', on_delete=models.PROTECT, related_name='empleados')
    puesto = models.ForeignKey(Puesto, on_delete=models.PROTECT, related_name='empleados')
    turno = models.ForeignKey('hr.Turno', on_delete=models.PROTECT, related_name='empleados_turno', blank=True, null=True)

    numero_empleado = models.CharField(max_length=20)
    nombre = models.CharField(max_length=150)
    apellido_paterno = models.CharField(max_length=150)
    apellido_materno = models.CharField(max_length=150, blank=True)
    fecha_nacimiento = models.DateField(blank=True, null=True)
    sexo = models.CharField(max_length=2, choices=SEXO_CHOICES, blank=True, null=True)
    estado_civil = models.CharField(max_length=20, choices=ESTADO_CIVIL_CHOICES, blank=True, null=True)
    nacionalidad = models.CharField(max_length=50, blank=True, null=True, default='Mexicana')
    lugar_nacimiento = models.CharField(max_length=150, blank=True, null=True)
    curp = models.CharField(max_length=18, unique=True, blank=True, null=True)
    rfc = models.CharField(max_length=13, unique=True, blank=True, null=True)
    nss = models.CharField(max_length=11, unique=True, blank=True, null=True)
    infonavit = models.CharField(max_length=20, blank=True, null=True)
    tipo_sangre = models.CharField(max_length=5, blank=True, null=True)
    alergias = models.TextField(blank=True, null=True)
    enfermedades_cronicas = models.TextField(blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    telefono = models.CharField(max_length=20, blank=True)

    calle = models.CharField(max_length=200, blank=True, null=True)
    numero_exterior = models.CharField(max_length=20, blank=True, null=True)
    numero_interior = models.CharField(max_length=20, blank=True, null=True)
    colonia = models.CharField(max_length=100, blank=True, null=True)
    codigo_postal = models.CharField(max_length=5, blank=True, null=True)
    ciudad = models.CharField(max_length=100, blank=True, null=True)
    estado = models.CharField(max_length=100, blank=True, null=True)

    banco = models.CharField(max_length=100, blank=True, null=True)
    cuenta_bancaria = models.CharField(max_length=20, blank=True, null=True)
    clabe = models.CharField(max_length=18, blank=True, null=True)
    moneda_pago = models.CharField(max_length=3, default='MXN', blank=True, null=True)

    nombre_emergencia = models.CharField(max_length=150, blank=True, null=True)
    parentesco_emergencia = models.CharField(max_length=50, blank=True, null=True)
    telefono_emergencia = models.CharField(max_length=20, blank=True, null=True)
    email_emergencia = models.EmailField(blank=True, null=True)

    foto_url = models.CharField(max_length=255, blank=True, null=True)
    observaciones = models.TextField(blank=True, null=True)

    fecha_ingreso = models.DateField()
    fecha_baja = models.DateField(blank=True, null=True)

    class Meta:
        db_table = "empleados"
        verbose_name = "Empleado"
        verbose_name_plural = "Empleados"
        constraints = [
            models.UniqueConstraint(fields=['empresa', 'numero_empleado'], name='unique_numero_empleado_empresa'),
        ]

    def __str__(self):
        return f"{self.nombre} {self.apellido_paterno}"

    @property
    def nombre_completo(self):
        return f"{self.nombre} {self.apellido_paterno} {self.apellido_materno}".strip()

    @property
    def antiguedad_meses(self):
        if not self.fecha_ingreso:
            return 0
        fin = self.fecha_baja or date.today()
        return (fin.year - self.fecha_ingreso.year) * 12 + (fin.month - self.fecha_ingreso.month)


class Area(StatusLifecycleModel):
    departamento = models.ForeignKey('nucleo.Departamento', on_delete=models.PROTECT, related_name='areas')
    nombre = models.CharField(max_length=150)
    codigo = models.CharField(max_length=20, blank=True, null=True, help_text='Clave corta del area')
    responsable = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='areas', blank=True, null=True)
    descripcion = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "areas"
        verbose_name = "Area"
        verbose_name_plural = "Areas"

    def __str__(self):
        return self.nombre


class Contrato(StatusLifecycleModel):
    TIPO_CHOICES = [
        ('indefinido', 'Indefinido'),
        ('determinado', 'Tiempo determinado'),
        ('prueba', 'Periodo de prueba'),
    ]
    ESTADO_CHOICES = [
        ('activo', 'Activo'),
        ('terminado', 'Terminado'),
        ('renovado', 'Renovado'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='contratos')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='indefinido')
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField(blank=True, null=True)
    salario = models.DecimalField(max_digits=10, decimal_places=2)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='activo')
    archivo_url = models.CharField(max_length=255, blank=True, null=True)
    creado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='contratos_creados', blank=True, null=True)
    observaciones = models.TextField(blank=True, null=True)
    prestaciones = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "contratos"
        verbose_name = "Contrato"
        verbose_name_plural = "Contratos"

    def __str__(self):
        return str(self.id)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.fecha_fin and self.fecha_fin < self.fecha_inicio:
            raise ValidationError({'fecha_fin': 'La fecha de fin no puede ser anterior a la de inicio.'})
        if self.estado == 'activo' and self.empleado_id:
            qs = Contrato.objects.filter(empleado=self.empleado, estado='activo')
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError({'estado': 'Este empleado ya tiene un contrato activo.'})


class Turno(StatusLifecycleModel):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.PROTECT, related_name='turnos')
    nombre = models.CharField(max_length=50)
    hora_entrada = models.TimeField()
    hora_salida = models.TimeField()
    dias_laborales = models.CharField(max_length=50, help_text='Ej: L,M,X,J,V', blank=True, null=True)
    tolerancia_retardo_minutos = models.PositiveIntegerField(default=5)
    horas_base_diarias = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('8.00'))
    descripcion = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "turnos"
        verbose_name = "Turno"
        verbose_name_plural = "Turnos"

    def __str__(self):
        return self.nombre

    def clean(self):
        from django.core.exceptions import ValidationError
        entrada_dt = datetime.combine(date.today(), self.hora_entrada)
        salida_dt = datetime.combine(date.today(), self.hora_salida)
        if salida_dt <= entrada_dt:
            raise ValidationError({'hora_salida': 'La hora de salida debe ser posterior a la de entrada.'})


class Calendario(models.Model):
    TIPO_CHOICES = [
        ('laborable', 'Laborable'),
        ('descanso', 'Descanso'),
        ('festivo', 'Festivo'),
        ('vacaciones', 'Vacaciones'),
    ]

    turno = models.ForeignKey(Turno, on_delete=models.PROTECT, related_name='calendario')
    fecha = models.DateField()
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='laborable')

    class Meta:
        db_table = "calendarios"
        verbose_name = "Calendario"
        verbose_name_plural = "Calendarios"

    def __str__(self):
        return str(self.id)


class Asistencia(models.Model):
    ESTADO_CHOICES = [
        ('puntual', 'Puntual'),
        ('retardo', 'Retardo'),
        ('falta', 'Falta'),
        ('justificada', 'Falta justificada'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='asistencias')
    turno = models.ForeignKey(Turno, on_delete=models.PROTECT, related_name='asistencias')
    fecha = models.DateField()
    hora_entrada = models.DateTimeField(blank=True, null=True)
    hora_salida = models.DateTimeField(blank=True, null=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='puntual')
    observaciones = models.TextField(blank=True, null=True)
    minutos_retardo = models.PositiveIntegerField(default=0)
    minutos_tolerancia = models.PositiveIntegerField(default=5)
    horas_normales = models.DecimalField(max_digits=4, decimal_places=2, blank=True, null=True)
    horas_extra = models.DecimalField(max_digits=4, decimal_places=2, blank=True, null=True)
    autorizado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='asistencias_autorizadas', blank=True, null=True)

    class Meta:
        db_table = "asistencias"
        verbose_name = "Asistencia"
        verbose_name_plural = "Asistencias"
        constraints = [
            models.UniqueConstraint(fields=['empleado', 'fecha'], name='unique_asistencia_empleado_fecha'),
        ]

    def __str__(self):
        return str(self.id)

    def save(self, *args, **kwargs):
        self._calcular_estado_y_horas()
        super().save(*args, **kwargs)

    def _calcular_estado_y_horas(self):
        hora_entrada = _con_zona_horaria(self.hora_entrada)
        hora_salida = _con_zona_horaria(self.hora_salida)

        if self.turno:
            self.minutos_tolerancia = self.turno.tolerancia_retardo_minutos
            base_diarias = self.turno.horas_base_diarias
        else:
            base_diarias = Decimal('8.00')

        if hora_entrada and self.turno:
            turno_entrada = _con_zona_horaria(datetime.combine(self.fecha, self.turno.hora_entrada))
            diff = (hora_entrada - turno_entrada).total_seconds() / 60
            if diff > self.minutos_tolerancia:
                self.minutos_retardo = int(diff - self.minutos_tolerancia)
                if self.estado in ('puntual',):
                    self.estado = 'retardo'
            elif diff < -30:
                self.minutos_retardo = 0
            else:
                self.minutos_retardo = 0
                if self.estado in ('retardo',):
                    pass

        if hora_salida and hora_entrada:
            total_segundos = (hora_salida - hora_entrada).total_seconds()
            total_horas = Decimal(str(total_segundos / 3600)).quantize(Decimal('0.01'))
            if total_horas > base_diarias:
                self.horas_normales = base_diarias
                self.horas_extra = (total_horas - base_diarias).quantize(Decimal('0.01'))
            else:
                self.horas_normales = total_horas
                self.horas_extra = Decimal('0.00')

            if not self.hora_entrada and self.estado in ('puntual', 'retardo'):
                self.estado = 'falta'


class ControlHoras(models.Model):
    TIPO_CHOICES = [
        ('normal', 'Normal'),
        ('extra', 'Extra'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='control_horas')
    asistencia = models.ForeignKey(Asistencia, on_delete=models.PROTECT, related_name='control_horas')
    op = models.ForeignKey('produccion.OrdenProduccion', on_delete=models.PROTECT, related_name='control_horas', null=True, blank=True)
    fecha = models.DateField()
    hora_inicio = models.DateTimeField()
    hora_fin = models.DateTimeField(blank=True, null=True)
    horas_trabajadas = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='normal')
    descripcion = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "control_horas"
        verbose_name = "Control Horas"
        verbose_name_plural = "Control Horas"

    def __str__(self):
        return str(self.id)

    def save(self, *args, **kwargs):
        if self.hora_inicio and self.hora_fin:
            total_segundos = (self.hora_fin - self.hora_inicio).total_seconds()
            self.horas_trabajadas = Decimal(str(total_segundos / 3600)).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)


class Vacaciones(models.Model):
    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('aprobado', 'Aprobado'),
        ('rechazado', 'Rechazado'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='vacaciones')
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField()
    dias_solicitados = models.PositiveIntegerField()
    motivo = models.TextField(blank=True, null=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    solicitado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='vacaciones_solicitadas', blank=True, null=True)
    fecha_solicitud = models.DateTimeField(default=timezone.now)
    autorizado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='vacaciones_autorizadas', blank=True, null=True)
    rechazado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='vacaciones_rechazadas', blank=True, null=True)
    fecha_aprobacion = models.DateTimeField(blank=True, null=True)
    fecha_rechazo = models.DateTimeField(blank=True, null=True)
    motivo_rechazo = models.TextField(blank=True, null=True)
    dias_disponibles_al_momento = models.PositiveIntegerField(blank=True, null=True)

    class Meta:
        db_table = "vacaciones"
        verbose_name = "Vacaciones"
        verbose_name_plural = "Vacaciones"

    def __str__(self):
        return str(self.id)


class PermisoAusencia(models.Model):
    TIPO_CHOICES = [
        ('permiso', 'Permiso'),
        ('incapacidad', 'Incapacidad'),
        ('falta_injustificada', 'Falta injustificada'),
    ]

    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('aprobado', 'Aprobado'),
        ('rechazado', 'Rechazado'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='permisos_ausencias')
    tipo = models.CharField(max_length=30, choices=TIPO_CHOICES, default='permiso')
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField()
    con_goce_sueldo = models.BooleanField(default=True)
    motivo = models.TextField(blank=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    solicitado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='permisos_solicitados', blank=True, null=True)
    fecha_solicitud = models.DateTimeField(default=timezone.now)
    autorizado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='permisos_autorizados', blank=True, null=True)
    rechazado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='permisos_rechazados', blank=True, null=True)
    fecha_aprobacion = models.DateTimeField(blank=True, null=True)
    fecha_rechazo = models.DateTimeField(blank=True, null=True)
    motivo_rechazo = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "permisos_ausencias"
        verbose_name = "Permiso Ausencia"
        verbose_name_plural = "Permisos Ausencias"

    def __str__(self):
        return str(self.id)


class Incidencia(StatusLifecycleModel):
    TIPO_CHOICES = [
        ('retardo', 'Retardo'),
        ('falta', 'Falta'),
        ('actitud', 'Actitud'),
        ('otro', 'Otro'),
    ]
    GRAVEDAD_CHOICES = [
        ('baja', 'Baja'),
        ('media', 'Media'),
        ('alta', 'Alta'),
    ]
    ESTADO_CHOICES = [
        ('abierto', 'Abierto'),
        ('cerrado', 'Cerrado'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='incidencias')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='otro')
    gravedad = models.CharField(max_length=20, choices=GRAVEDAD_CHOICES, default='baja')
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='abierto')
    fecha = models.DateField()
    descripcion = models.TextField(blank=True)
    acciones_tomadas = models.TextField(blank=True, null=True)
    reportado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='incidencias_reportadas', blank=True, null=True)
    fecha_reporte = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "incidencias"
        verbose_name = "Incidencia"
        verbose_name_plural = "Incidencias"

    def __str__(self):
        return str(self.id)


class Evaluacion(models.Model):
    TIPO_CHOICES = [
        ('desempeno', 'Desempeño'),
        ('competencias', 'Competencias'),
        ('objetivo', 'Por objetivos'),
    ]
    PERIODO_CHOICES = [
        ('trimestral', 'Trimestral'),
        ('semestral', 'Semestral'),
        ('anual', 'Anual'),
    ]
    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('completada', 'Completada'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='evaluaciones')
    evaluador = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='evaluaciones_realizadas', blank=True, null=True)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='desempeno')
    periodo = models.CharField(max_length=20, choices=PERIODO_CHOICES, default='anual')
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    fecha = models.DateField()
    puntaje = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    comentarios = models.TextField(blank=True)

    class Meta:
        db_table = "evaluaciones"
        verbose_name = "Evaluacion"
        verbose_name_plural = "Evaluaciones"

    def __str__(self):
        return str(self.id)


class Capacitacion(models.Model):
    ESTADO_CHOICES = [
        ('inscrito', 'Inscrito'),
        ('en_curso', 'En curso'),
        ('finalizado', 'Finalizado'),
        ('cancelado', 'Cancelado'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='capacitaciones')
    nombre = models.CharField(max_length=255)
    institucion = models.CharField(max_length=255, blank=True)
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField(blank=True, null=True)
    horas = models.PositiveIntegerField(blank=True, null=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='inscrito')
    calificacion = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    constancia_url = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = "capacitaciones"
        verbose_name = "Capacitacion"
        verbose_name_plural = "Capacitaciones"

    def __str__(self):
        return str(self.id)


class Nomina(models.Model):
    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('pagada', 'Pagada'),
        ('cancelada', 'Cancelada'),
    ]

    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.PROTECT, related_name='nominas')
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.PROTECT, related_name='nominas')
    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='nominas')
    periodo_inicio = models.DateField()
    periodo_fin = models.DateField()
    fecha_pago = models.DateField(blank=True, null=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    salario_base = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    dias_pagados = models.PositiveIntegerField(default=15)
    horas_extra_pagadas = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    total_percepciones = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    total_deducciones = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    neto = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    creado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='nominas_creadas', blank=True, null=True)
    observaciones = models.TextField(blank=True, null=True)
    fecha_generacion = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "nominas"
        verbose_name = "Nomina"
        verbose_name_plural = "Nominas"

    def __str__(self):
        return str(self.id)

    def save(self, *args, **kwargs):
        es_nuevo = self.pk is None
        super().save(*args, **kwargs)
        if not es_nuevo:
            self._recalcular_totales(guardar=False)
            super().save(update_fields=['total_percepciones', 'total_deducciones', 'neto'])

    def _recalcular_totales(self, guardar=True):
        detalles = self.detalles.all()
        perc = detalles.filter(tipo='percepcion').aggregate(s=Sum('monto'))['s'] or Decimal('0.00')
        ded = detalles.filter(tipo='deduccion').aggregate(s=Sum('monto'))['s'] or Decimal('0.00')
        self.total_percepciones = perc
        self.total_deducciones = ded
        self.neto = perc - ded
        if guardar:
            self.save(update_fields=['total_percepciones', 'total_deducciones', 'neto'])


class NominaDetalle(models.Model):
    TIPO_CHOICES = [
        ('percepcion', 'Percepcion'),
        ('deduccion', 'Deduccion'),
    ]

    nomina = models.ForeignKey(Nomina, on_delete=models.PROTECT, related_name='detalles')
    codigo = models.CharField(max_length=20, blank=True, null=True)
    concepto = models.CharField(max_length=255)
    tipo = models.CharField(max_length=15, choices=TIPO_CHOICES)
    cantidad = models.PositiveIntegerField(default=1)
    unidad = models.CharField(max_length=20, default='MXN', blank=True, null=True)
    monto = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "nomina_detalle"
        verbose_name = "Nomina Detalle"
        verbose_name_plural = "Nominas Detalle"

    def __str__(self):
        return str(self.id)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.nomina_id:
            self.nomina._recalcular_totales()


class Productividad(models.Model):
    ESTADO_CHOICES = [
        ('borrador', 'Borrador'),
        ('confirmado', 'Confirmado'),
    ]

    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.PROTECT, related_name='productividad')
    departamento = models.ForeignKey('nucleo.Departamento', on_delete=models.PROTECT, related_name='productividad')
    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='productividad')
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='borrador')
    creado_por = models.ForeignKey('usuarios.Usuario', on_delete=models.PROTECT, related_name='productividad_creada', blank=True, null=True)

    fecha = models.DateField()
    meta = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    meta_unidad = models.ForeignKey('nucleo.UnidadMedida', on_delete=models.PROTECT, related_name='productividad_detalles')
    resultado = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    descripcion = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "productividad"
        verbose_name = "Productividad"
        verbose_name_plural = "Productividad"

    def __str__(self):
        return str(self.id)


class ProductividadDetalle(models.Model):
    productividad = models.ForeignKey(Productividad, on_delete=models.PROTECT, related_name='detalles')
    fecha = models.DateField()
    cantidad = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "productividad_detalle"
        verbose_name = "Productividad Detalle"
        verbose_name_plural = "Productividad Detalle"

    def __str__(self):
        return str(self.id)
