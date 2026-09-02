from django.conf import settings
from django.db import models


class Notificacion(models.Model):
    empresa = models.ForeignKey(
        "nucleo.Empresa",
        on_delete=models.PROTECT,
        related_name="notificaciones",
    )
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notificaciones",
    )
    titulo = models.CharField(max_length=200)
    mensaje = models.TextField()
    modulo = models.CharField(max_length=50, db_index=True)
    tipo = models.CharField(max_length=50, db_index=True)
    leido = models.BooleanField(default=False, db_index=True)
    data = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    leido_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notificaciones"
        verbose_name = "Notificación"
        verbose_name_plural = "Notificaciones"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["usuario", "leido", "-created_at"]),
            models.Index(fields=["empresa", "modulo"]),
        ]

    def __str__(self):
        return f"#{self.id} [{self.modulo}] {self.titulo} -> {self.usuario_id}"
