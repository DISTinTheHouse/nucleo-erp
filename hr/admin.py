from django.contrib import admin

from hr.models import (
    Area,
    Asistencia,
    Calendario,
    Capacitacion,
    Contrato,
    ControlHoras,
    Empleado,
    Evaluacion,
    Incidencia,
    Nomina,
    NominaDetalle,
    PermisoAusencia,
    Productividad,
    ProductividadDetalle,
    Puesto,
    Turno,
    Vacaciones,
)


@admin.display(description="Empleado")
def empleado_display(obj):
    if not obj:
        return "-"
    return f"{obj.nombre} {obj.apellido_paterno} {obj.apellido_materno}".strip()


@admin.display(description="Empresa")
def empresa_display(obj):
    return obj.empresa.codigo if getattr(obj, "empresa", None) else "-"


class AreaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo", "departamento", "responsable", "activo")
    list_filter = ("activo", "departamento__empresa", "departamento__sucursal")
    search_fields = ("nombre", "codigo", "descripcion", "responsable__nombre", "responsable__apellido_paterno")
    list_select_related = ("departamento", "responsable")


class PuestoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "empresa", "area", "salario_base", "activo")
    list_filter = ("activo", "empresa", "area")
    search_fields = ("nombre", "descripcion", "empresa__codigo", "area__nombre")
    list_select_related = ("empresa", "area")


class EmpleadoAdmin(admin.ModelAdmin):
    list_display = (
        "numero_empleado",
        "nombre_completo",
        "empresa",
        "sucursal",
        "departamento",
        "puesto",
        "fecha_ingreso",
        "activo",
    )
    list_display_links = ("numero_empleado", "nombre_completo")
    list_filter = ("activo", "empresa", "sucursal", "departamento", "puesto")
    search_fields = (
        "numero_empleado",
        "nombre",
        "apellido_paterno",
        "apellido_materno",
        "rfc",
        "curp",
        "email",
        "telefono",
        "empresa__codigo",
        "sucursal__nombre",
        "departamento__nombre",
        "puesto__nombre",
    )
    list_select_related = ("empresa", "sucursal", "departamento", "puesto")

    @admin.display(description="Nombre completo")
    def nombre_completo(self, obj):
        return f"{obj.nombre} {obj.apellido_paterno} {obj.apellido_materno}".strip()


class ContratoAdmin(admin.ModelAdmin):
    list_display = ("empleado", "tipo", "fecha_inicio", "fecha_fin", "salario", "estado")
    list_filter = ("estado", "tipo", "fecha_inicio", "fecha_fin")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "empleado__apellido_paterno", "archivo_url")
    list_select_related = ("empleado",)


class TurnoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "empresa", "hora_entrada", "hora_salida", "dias_laborales")
    list_filter = ("empresa", "dias_laborales")
    search_fields = ("nombre", "empresa__codigo", "dias_laborales")
    list_select_related = ("empresa",)


class CalendarioAdmin(admin.ModelAdmin):
    list_display = ("turno", "fecha", "tipo")
    list_filter = ("tipo", "turno__empresa", "fecha")
    search_fields = ("turno__nombre", "tipo")
    list_select_related = ("turno",)


class AsistenciaAdmin(admin.ModelAdmin):
    list_display = ("empleado", "turno", "fecha", "hora_entrada", "hora_salida", "estado")
    list_filter = ("estado", "fecha", "turno__empresa", "empleado__empresa")
    search_fields = (
        "empleado__numero_empleado",
        "empleado__nombre",
        "empleado__apellido_paterno",
        "turno__nombre",
        "observaciones",
    )
    list_select_related = ("empleado", "turno")


class ControlHorasAdmin(admin.ModelAdmin):
    list_display = ("empleado", "asistencia", "fecha", "hora_inicio", "hora_fin", "horas_trabajadas", "tipo")
    list_filter = ("tipo", "fecha", "empleado__empresa")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "asistencia__estado")
    list_select_related = ("empleado", "asistencia", "op")


class VacacionesAdmin(admin.ModelAdmin):
    list_display = ("empleado", "fecha_inicio", "fecha_fin", "dias_solicitados", "estado")
    list_filter = ("estado", "fecha_inicio", "fecha_fin")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "motivo")
    list_select_related = ("empleado",)


class PermisoAusenciaAdmin(admin.ModelAdmin):
    list_display = ("empleado", "tipo", "fecha_inicio", "fecha_fin", "con_goce_sueldo", "estado")
    list_filter = ("estado", "tipo", "con_goce_sueldo", "fecha_inicio")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "motivo")
    list_select_related = ("empleado",)


class IncidenciaAdmin(admin.ModelAdmin):
    list_display = ("empleado", "tipo", "fecha", "descripcion")
    list_filter = ("tipo", "fecha")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "descripcion")
    list_select_related = ("empleado",)


class EvaluacionAdmin(admin.ModelAdmin):
    list_display = ("empleado", "evaluador", "fecha", "puntaje")
    list_filter = ("fecha", "puntaje")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "comentarios", "evaluador__nombre")
    list_select_related = ("empleado", "evaluador")


class CapacitacionAdmin(admin.ModelAdmin):
    list_display = ("empleado", "nombre", "institucion", "fecha_inicio", "fecha_fin", "horas")
    list_filter = ("fecha_inicio", "fecha_fin", "empleado__empresa")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "nombre", "institucion")
    list_select_related = ("empleado",)


class NominaAdmin(admin.ModelAdmin):
    list_display = ("empresa", "sucursal", "empleado", "periodo_inicio", "periodo_fin", "fecha_pago", "estado", "neto")
    list_filter = ("estado", "empresa", "sucursal", "periodo_inicio", "periodo_fin")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "empresa__codigo", "sucursal__nombre")
    list_select_related = ("empresa", "sucursal", "empleado")


class NominaDetalleAdmin(admin.ModelAdmin):
    list_display = ("nomina", "concepto", "tipo", "monto")
    list_filter = ("tipo", "nomina__estado", "nomina__empresa")
    search_fields = ("concepto", "nomina__empleado__numero_empleado", "nomina__empleado__nombre")
    list_select_related = ("nomina", "nomina__empleado")


class ProductividadAdmin(admin.ModelAdmin):
    list_display = ("empresa", "departamento", "empleado", "fecha", "meta", "resultado")
    list_filter = ("fecha", "empresa", "departamento", "empleado")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "descripcion", "empresa__codigo")
    list_select_related = ("empresa", "departamento", "empleado", "meta_unidad")


class ProductividadDetalleAdmin(admin.ModelAdmin):
    list_display = ("productividad", "fecha", "cantidad")
    list_filter = ("fecha", "productividad__empresa", "productividad__empleado")
    search_fields = ("productividad__empleado__numero_empleado", "productividad__empleado__nombre")
    list_select_related = ("productividad", "productividad__empleado")


admin.site.register(Area, AreaAdmin)
admin.site.register(Puesto, PuestoAdmin)
admin.site.register(Empleado, EmpleadoAdmin)
admin.site.register(Contrato, ContratoAdmin)
admin.site.register(Turno, TurnoAdmin)
admin.site.register(Calendario, CalendarioAdmin)
admin.site.register(Asistencia, AsistenciaAdmin)
admin.site.register(ControlHoras, ControlHorasAdmin)
admin.site.register(Vacaciones, VacacionesAdmin)
admin.site.register(PermisoAusencia, PermisoAusenciaAdmin)
admin.site.register(Incidencia, IncidenciaAdmin)
admin.site.register(Evaluacion, EvaluacionAdmin)
admin.site.register(Capacitacion, CapacitacionAdmin)
admin.site.register(Nomina, NominaAdmin)
admin.site.register(NominaDetalle, NominaDetalleAdmin)
admin.site.register(Productividad, ProductividadAdmin)
admin.site.register(ProductividadDetalle, ProductividadDetalleAdmin)

