from django.db import models
from nucleo.models import StatusLifecycleModel

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
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.PROTECT, related_name='empleados')
    sucursal = models.ForeignKey('nucleo.Sucursal', on_delete=models.PROTECT, related_name='empleados')
    departamento = models.ForeignKey('nucleo.Departamento', on_delete=models.PROTECT, related_name='empleados')
    puesto = models.ForeignKey(Puesto, on_delete=models.PROTECT, related_name='empleados')

    numero_empleado = models.CharField(max_length=20, unique=True)
    nombre = models.CharField(max_length=150)
    apellido_paterno = models.CharField(max_length=150)
    apellido_materno = models.CharField(max_length=150, blank=True)
    fecha_nacimiento = models.DateField(blank=True, null=True)
    curp = models.CharField(max_length=18, unique=True, blank=True, null=True)
    rfc = models.CharField(max_length=13, unique=True, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    telefono = models.CharField(max_length=20, blank=True)
    fecha_ingreso = models.DateField()
    fecha_baja = models.DateField(blank=True, null=True)
 
    class Meta:
        db_table = "empleados"
        verbose_name = "Empleado"
        verbose_name_plural = "Empleados"

    def __str__(self):
        return f"{self.nombre} {self.apellido_paterno}"

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

class Contrato(models.Model):
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

    class Meta:
        db_table = "contratos"
        verbose_name = "Contrato"
        verbose_name_plural = "Contratos"

    def __str__(self):
        return str(self.id)

class Turno(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.PROTECT, related_name='turnos')
    nombre = models.CharField(max_length=50)
    hora_entrada = models.TimeField()
    hora_salida = models.TimeField()
    dias_laborales = models.CharField(max_length=50, help_text='Ej: L,M,X,J,V', blank=True, null=True)

    class Meta:
        db_table = "turnos"
        verbose_name = "Turno"
        verbose_name_plural = "Turnos"

    def __str__(self):
        return self.nombre

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

    class Meta:
        db_table = "asistencias"
        verbose_name = "Asistencia"
        verbose_name_plural = "Asistencias"

    def __str__(self):
        return str(self.id)

class ControlHoras(models.Model):
    TIPO_CHOICES = [
        ('normal', 'Normal'),
        ('extra', 'Extra'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='control_horas')
    asistencia = models.ForeignKey(Asistencia, on_delete=models.PROTECT, related_name='control_horas')
    op = models.ForeignKey('produccion.OrdenProduccion', on_delete=models.PROTECT, related_name='control_horas', null=True, blank=True)
    # TODO:  proyecto = models.ForeignKey('psa.Proyecto', on_delete=models.PROTECT, related_name='control_horas')
    fecha = models.DateField()
    hora_inicio = models.DateTimeField()
    hora_fin = models.DateTimeField(blank=True, null=True)
    horas_trabajadas = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='normal')

    class Meta:
        db_table = "control_horas"
        verbose_name = "Control Horas"
        verbose_name_plural = "Control Horas"

    def __str__(self):
        return str(self.id)

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

    class Meta:
        db_table = "permisos_ausencias"
        verbose_name = "Permiso Ausencia"
        verbose_name_plural = "Permisos Ausencias"

    def __str__(self):
        return str(self.id)

class Incidencia(models.Model):
    TIPO_CHOICES = [
        ('retardo', 'Retardo'),
        ('falta', 'Falta'),
        ('actitud', 'Actitud'),
        ('otro', 'Otro'),
    ]

    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='incidencias')
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default='otro')
    fecha = models.DateField()
    descripcion = models.TextField(blank=True)

    class Meta:
        db_table = "incidencias"
        verbose_name = "Incidencia"
        verbose_name_plural = "Incidencias"

    def __str__(self):
        return str(self.id)

class Evaluacion(models.Model):
    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='evaluaciones')
    evaluador = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='evaluaciones_realizadas', blank=True, null=True)
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
    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='capacitaciones')
    nombre = models.CharField(max_length=255)
    institucion = models.CharField(max_length=255, blank=True)
    fecha_inicio = models.DateField()
    fecha_fin = models.DateField(blank=True, null=True)
    horas = models.PositiveIntegerField(blank=True, null=True)

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
    total_percepciones = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    total_deducciones = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    neto = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)

    class Meta:
        db_table = "nominas"
        verbose_name = "Nomina"
        verbose_name_plural = "Nominas"

    def __str__(self):
        return str(self.id)

class NominaDetalle(models.Model):
    TIPO_CHOICES = [
        ('percepcion', 'Percepcion'),
        ('deduccion', 'Deduccion'),
    ]

    nomina = models.ForeignKey(Nomina, on_delete=models.PROTECT, related_name='detalles')
    concepto = models.CharField(max_length=255)
    tipo = models.CharField(max_length=15, choices=TIPO_CHOICES)
    monto = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "nomina_detalle"
        verbose_name = "Nomina Detalle"
        verbose_name_plural = "Nominas Detalle"

    def __str__(self):
        return str(self.id)

class Productividad(models.Model):
    empresa = models.ForeignKey('nucleo.Empresa', on_delete=models.PROTECT, related_name='productividad')
    departamento = models.ForeignKey('nucleo.Departamento', on_delete=models.PROTECT, related_name='productividad')
    empleado = models.ForeignKey(Empleado, on_delete=models.PROTECT, related_name='productividad')

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
    # TODO:  proyecto = models.ForeignKey('psa.Proyecto', on_delete=models.PROTECT, related_name='control_horas')

    class Meta:
        db_table = "productividad_detalle"
        verbose_name = "Productividad Detalle"
        verbose_name = "Productividad Detalle"

    def __str__(self):
        return str(self.id)