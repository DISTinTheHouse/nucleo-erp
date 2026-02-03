from django.db import models
from django.contrib.auth.models import AbstractUser


# class Usuario(AbstractUser):
#     class Estatus(models.TextChoices):
#         ACTIVO = "activo", "Activo"
#         BLOQUEADO = "bloqueado", "Bloqueado"

#     empresa = models.ForeignKey(
#         "nucleo.Empresa",
#         on_delete=models.PROTECT,
#         related_name="usuarios",
#         null=True,
#         blank=True,
#     )

#     sucursal_default = models.ForeignKey(
#         "nucleo.Sucursal",
#         on_delete=models.SET_NULL,
#         related_name="usuarios_default",
#         null=True,
#         blank=True,
#     )

#     telefono = models.CharField(max_length=30, blank=True, null=True)

#     is_admin_empresa = models.BooleanField(default=False)
#     estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)

#     two_factor_enabled = models.BooleanField(default=False)
#     avatar_url = models.URLField(blank=True, null=True)
#     preferencias_json = models.JSONField(default=dict, blank=True)

#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)

#     class Meta:
#         db_table = "usuarios"
#         verbose_name = "Usuario"
#         verbose_name_plural = "Usuarios"
#         indexes = [
#             models.Index(fields=["empresa"]),
#             models.Index(fields=["estatus"]),
#         ]

#     def __str__(self):
#         return self.username
    

class Usuario(models.Model):
    # claves primarias / foraneas
    id_usuario = models.BigAutoField(primary_key=True)
    id_empresa = models.BigIntegerField(db_index=True)
    id_sucursal_default = models.BigIntegerField(null=True, blank=True)

    # credenciales
    username = models.CharField(max_length=50)
    email = models.EmailField(max_length=255)
    password_hash = models.CharField(max_length=255)

    # informacion personal
    nombre = models.CharField(max_length=100)
    apellidos = models.CharField(max_length=150)
    telefono = models.CharField(max_length=20, null=True, blank=True)

    # Roles / estados
    is_admin_empresa = models.BooleanField(default=False)

    ESTATUS_CHOICES = {
        (1, 'Activo'),
        (2, 'Bloqueado'),
        (3, 'Suspendido'),
    }

    estatus = models.PositiveSmallIntegerField(choices=ESTATUS_CHOICES, default=1)

    # Auditoria
    last_login_at = models.DateTimeField(null=True, blank=True)

    # opcionales
    two_factor_enabled = models.BooleanField(default=False)
    avatar_url = models.TextField(null=True, blank=True)
    preferencias_json = models.JSONField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Meta
    class Meta:
        db_table = 'usuarios'
        verbose_name = 'Usuario'
        verbose_name_plural = 'Usuarios'
        constraints = [
            models.UniqueConstraint(
                fields=['id_empresa', 'username'],
                name='uq_usuario_username_empresa'
            ),
            models.UniqueConstraint(
                fields=['id_empresa', 'email'],
                name='uq_usuario_email_empresa'
            ),
        ]
        indexes = [
            models.Index(fields=['id_empresa'], name='idx_usuario_empresa'),
            models.Index(fields=['estatus'], name='idx_usuario_estatus'),
        ]
    
    def __str__(self):
        return f'{self.username} ({self.email})'

