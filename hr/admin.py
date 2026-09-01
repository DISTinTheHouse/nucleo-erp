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
        "turno",
        "fecha_ingreso",
        "activo",
    )
    list_display_links = ("numero_empleado", "nombre_completo")
    list_filter = ("activo", "empresa", "sucursal", "departamento", "puesto", "turno", "sexo", "estado_civil")
    search_fields = (
        "numero_empleado",
        "nombre",
        "apellido_paterno",
        "apellido_materno",
        "rfc",
        "curp",
        "nss",
        "email",
        "telefono",
        "banco",
        "clabe",
        "empresa__codigo",
        "sucursal__nombre",
        "departamento__nombre",
        "puesto__nombre",
    )
    list_select_related = ("empresa", "sucursal", "departamento", "puesto", "turno")
    fieldsets = (
        (None, {
            "fields": ("empresa", "sucursal", "departamento", "puesto", "turno", "numero_empleado", "activo")
        }),
        ("Información personal", {
            "fields": ("nombre", "apellido_paterno", "apellido_materno", "fecha_nacimiento", "lugar_nacimiento", "sexo", "estado_civil", "nacionalidad", "curp", "rfc", "nss", "infonavit", "tipo_sangre", "alergias", "enfermedades_cronicas")
        }),
        ("Contacto", {
            "fields": ("email", "telefono")
        }),
        ("Domicilio", {
            "fields": ("calle", "numero_exterior", "numero_interior", "colonia", "codigo_postal", "ciudad", "estado")
        }),
        ("Datos bancarios", {
            "fields": ("banco", "cuenta_bancaria", "clabe", "moneda_pago")
        }),
        ("Contacto de emergencia", {
            "fields": ("nombre_emergencia", "parentesco_emergencia", "telefono_emergencia", "email_emergencia")
        }),
        ("Laboral", {
            "fields": ("fecha_ingreso", "fecha_baja", "foto_url", "observaciones")
        }),
    )

    @admin.display(description="Nombre completo")
    def nombre_completo(self, obj):
        return f"{obj.nombre} {obj.apellido_paterno} {obj.apellido_materno}".strip()


class ContratoAdmin(admin.ModelAdmin):
    list_display = ("empleado", "tipo", "fecha_inicio", "fecha_fin", "salario", "estado", "activo", "creado_por")
    list_filter = ("estado", "activo", "tipo", "fecha_inicio", "fecha_fin")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "archivo_url", "observaciones", "prestaciones")
    list_select_related = ("empleado", "creado_por")


class TurnoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "empresa", "hora_entrada", "hora_salida", "horas_base_diarias", "tolerancia_retardo_minutos", "dias_laborales", "activo")
    list_filter = ("activo", "empresa", "dias_laborales")
    search_fields = ("nombre", "empresa__codigo", "dias_laborales", "descripcion")
    list_select_related = ("empresa",)


class CalendarioAdmin(admin.ModelAdmin):
    list_display = ("turno", "fecha", "tipo")
    list_filter = ("tipo", "turno__empresa", "fecha")
    search_fields = ("turno__nombre", "tipo")
    list_select_related = ("turno",)


class AsistenciaAdmin(admin.ModelAdmin):
    list_display = ("empleado", "turno", "fecha", "hora_entrada", "hora_salida", "estado", "minutos_retardo", "horas_normales", "horas_extra")
    list_filter = ("estado", "fecha", "turno__empresa", "empleado__empresa")
    search_fields = (
        "empleado__numero_empleado",
        "empleado__nombre",
        "empleado__apellido_paterno",
        "turno__nombre",
        "observaciones",
    )
    list_select_related = ("empleado", "turno", "autorizado_por")


class ControlHorasAdmin(admin.ModelAdmin):
    list_display = ("empleado", "asistencia", "fecha", "hora_inicio", "hora_fin", "horas_trabajadas", "tipo")
    list_filter = ("tipo", "fecha", "empleado__empresa")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "asistencia__estado", "descripcion")
    list_select_related = ("empleado", "asistencia", "op")


class VacacionesAdmin(admin.ModelAdmin):
    list_display = ("empleado", "fecha_inicio", "fecha_fin", "dias_solicitados", "estado", "fecha_solicitud", "autorizado_por", "rechazado_por")
    list_filter = ("estado", "fecha_inicio", "fecha_fin", "fecha_solicitud")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "motivo", "motivo_rechazo")
    list_select_related = ("empleado", "solicitado_por", "autorizado_por", "rechazado_por")


class PermisoAusenciaAdmin(admin.ModelAdmin):
    list_display = ("empleado", "tipo", "fecha_inicio", "fecha_fin", "con_goce_sueldo", "estado", "fecha_solicitud", "autorizado_por", "rechazado_por")
    list_filter = ("estado", "tipo", "con_goce_sueldo", "fecha_inicio", "fecha_solicitud")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "motivo", "motivo_rechazo")
    list_select_related = ("empleado", "solicitado_por", "autorizado_por", "rechazado_por")


class IncidenciaAdmin(admin.ModelAdmin):
    list_display = ("empleado", "tipo", "gravedad", "estado", "fecha", "reportado_por", "fecha_reporte", "descripcion_corta")
    list_filter = ("tipo", "gravedad", "estado", "activo", "fecha", "fecha_reporte")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "descripcion", "acciones_tomadas")
    list_select_related = ("empleado", "reportado_por")

    @admin.display(description="Descripción")
    def descripcion_corta(self, obj):
        if not obj.descripcion:
            return "-"
        return (obj.descripcion[:80] + "...") if len(obj.descripcion) > 80 else obj.descripcion


class EvaluacionAdmin(admin.ModelAdmin):
    list_display = ("empleado", "evaluador", "tipo", "periodo", "estado", "fecha", "puntaje")
    list_filter = ("tipo", "periodo", "estado", "fecha", "puntaje")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "comentarios", "evaluador__nombre")
    list_select_related = ("empleado", "evaluador")


class CapacitacionAdmin(admin.ModelAdmin):
    list_display = ("empleado", "nombre", "institucion", "estado", "fecha_inicio", "fecha_fin", "horas", "calificacion")
    list_filter = ("estado", "fecha_inicio", "fecha_fin", "empleado__empresa")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "nombre", "institucion", "constancia_url")
    list_select_related = ("empleado",)


class NominaAdmin(admin.ModelAdmin):
    list_display = ("empresa", "sucursal", "empleado", "periodo_inicio", "periodo_fin", "fecha_pago", "estado", "salario_base", "total_percepciones", "total_deducciones", "neto")
    list_filter = ("estado", "empresa", "sucursal", "periodo_inicio", "periodo_fin")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "empresa__codigo", "sucursal__nombre", "observaciones")
    list_select_related = ("empresa", "sucursal", "empleado", "creado_por")


class NominaDetalleAdmin(admin.ModelAdmin):
    list_display = ("nomina", "codigo", "concepto", "tipo", "cantidad", "unidad", "monto")
    list_filter = ("tipo", "nomina__estado", "nomina__empresa")
    search_fields = ("codigo", "concepto", "nomina__empleado__numero_empleado", "nomina__empleado__nombre")
    list_select_related = ("nomina", "nomina__empleado")


class ProductividadAdmin(admin.ModelAdmin):
    list_display = ("empresa", "departamento", "empleado", "estado", "fecha", "meta", "meta_unidad", "resultado")
    list_filter = ("estado", "fecha", "empresa", "departamento", "empleado", "meta_unidad")
    search_fields = ("empleado__numero_empleado", "empleado__nombre", "descripcion", "empresa__codigo")
    list_select_related = ("empresa", "departamento", "empleado", "meta_unidad", "creado_por")


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
