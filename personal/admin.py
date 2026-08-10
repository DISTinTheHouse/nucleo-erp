from django.contrib import admin

from personal.models import (
    Puesto,
    Empleado,
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

admin.site.register(Puesto)
admin.site.register(Empleado)
admin.site.register(Contrato)
admin.site.register(Turno)
admin.site.register(Calendario)
admin.site.register(Asistencia)
admin.site.register(ControlHoras)
admin.site.register(Vacaciones)
admin.site.register(PermisoAusencia)
admin.site.register(Incidencia)
admin.site.register(Evaluacion)
admin.site.register(Capacitacion)
admin.site.register(Nomina)
admin.site.register(NominaDetalle)
admin.site.register(Productividad)
admin.site.register(ProductividadDetalle)

