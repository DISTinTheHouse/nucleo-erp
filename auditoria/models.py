from django.db import models

class AuditoriaEvento(models.Model):
    id_evento = models.BigAutoField(primary_key=True)

    empresa = models.ForeignKey("nucleo.Empresa", on_delete=models.PROTECT, related_name="auditorias")
    usuario = models.ForeignKey(
        "usuarios.Usuario",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auditorias",
    )

    modulo = models.CharField(max_length=60)  # inventarios, ventas, etc.
    accion = models.CharField(max_length=30)  # CREATE/UPDATE/DELETE/LOGIN/EXPORT
    tabla = models.CharField(max_length=80, blank=True, null=True)
    id_registro = models.CharField(max_length=80, blank=True, null=True)

    antes_json = models.JSONField(blank=True, null=True)
    despues_json = models.JSONField(blank=True, null=True)

    ip = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "auditoria_eventos"
        verbose_name = "Auditoría"
        verbose_name_plural = "Auditorías"
        indexes = [
            models.Index(fields=["empresa", "created_at"]),
            models.Index(fields=["modulo", "accion"]),
        ]

    def __str__(self):
        return f"{self.empresa.codigo} {self.modulo}:{self.accion}"


class BitacoraDocumento(models.Model):
    """Histórico ligero de documentos del ERP: una fila por evento (alta, cambio
    de estatus, baja, restauración o borrado). Sin JSON: solo lo necesario para
    filtrar y reconstruir la línea de tiempo de un documento. La llenan las
    señales de ``auditoria.bitacora``; la API solo la lee."""

    class Accion(models.TextChoices):
        CREAR = "crear", "Creado"
        ESTATUS = "estatus", "Cambio de estatus"
        BAJA = "baja", "Dado de baja"
        RESTAURAR = "restaurar", "Restaurado"
        ELIMINAR = "eliminar", "Eliminado"

    id = models.BigAutoField(primary_key=True)
    # Enteros sin FK: la bitácora sobrevive al borrado de lo que registra.
    empresa_id = models.IntegerField()
    sucursal_id = models.IntegerField(null=True, blank=True)
    tipo_documento = models.CharField(max_length=30)
    documento_id = models.CharField(max_length=40)
    folio = models.CharField(max_length=60, blank=True, default="")
    accion = models.CharField(max_length=12, choices=Accion.choices)
    estatus_anterior = models.CharField(max_length=40, blank=True, default="")
    estatus_nuevo = models.CharField(max_length=40, blank=True, default="")
    total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    moneda_id = models.IntegerField(null=True, blank=True)
    origen_tipo = models.CharField(max_length=30, blank=True, default="")
    origen_id = models.IntegerField(null=True, blank=True)
    usuario = models.ForeignKey(
        "usuarios.Usuario", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", db_constraint=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bitacora_documentos"
        verbose_name = "Bitácora de documento"
        verbose_name_plural = "Bitácora de documentos"
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["empresa_id", "-id"], name="bitdoc_empresa_id"),
            models.Index(fields=["tipo_documento", "documento_id"], name="bitdoc_documento"),
            models.Index(fields=["origen_tipo", "origen_id"], name="bitdoc_origen"),
        ]

    def __str__(self):
        return f"{self.tipo_documento} {self.folio or self.documento_id} {self.accion}"
