from django.db import models
from django.contrib.auth.models import AbstractUser


class Usuario(AbstractUser):
    class Estatus(models.TextChoices):
        ACTIVO = "activo", "Activo"
        BLOQUEADO = "bloqueado", "Bloqueado"

    empresa = models.ForeignKey(
        "nucleo.Empresa",
        on_delete=models.PROTECT,
        related_name="usuarios",
        null=True,
        blank=True,
    )

    sucursal_default = models.ForeignKey(
        "nucleo.Sucursal",
        on_delete=models.SET_NULL,
        related_name="usuarios_default",
        null=True,
        blank=True,
    )

    telefono = models.CharField(max_length=30, blank=True, null=True)

    is_admin_empresa = models.BooleanField(default=False)
    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)

    two_factor_enabled = models.BooleanField(default=False)
    avatar_url = models.URLField(blank=True, null=True)
    preferencias_json = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "usuarios"
        verbose_name = "Usuario"
        verbose_name_plural = "Usuarios"
        indexes = [
            models.Index(fields=["empresa"]),
            models.Index(fields=["estatus"]),
        ]

    def __str__(self):
        return self.username
