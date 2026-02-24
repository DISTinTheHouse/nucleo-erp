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

    # Empresas a las que el usuario tiene acceso (Multi-Tenant)
    empresas = models.ManyToManyField(
        "nucleo.Empresa",
        related_name="usuarios_autorizados",
        blank=True,
        help_text="Empresas a las que el usuario tiene acceso."
    )

    sucursal_default = models.ForeignKey(
        "nucleo.Sucursal",
        on_delete=models.SET_NULL,
        related_name="usuarios_default",
        null=True,
        blank=True,
    )

    # Para controlar a qué sucursales tiene acceso el usuario (Scope de Datos)
    sucursales = models.ManyToManyField(
        "nucleo.Sucursal",
        related_name="usuarios_permitidos",
        blank=True,
        help_text="Sucursales a las que el usuario tiene acceso para operar."
    )

    # Para limitar la visualización por área funcional dentro de las sucursales
    departamentos = models.ManyToManyField(
        "nucleo.Departamento",
        related_name="usuarios_permitidos",
        blank=True,
        help_text="Departamentos específicos a los que el usuario tiene acceso."
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
            # models.Index(fields=["empresa"]),
            models.Index(fields=["estatus"]),
        ]

    def save(self, *args, **kwargs):
        # Sincronizar is_active con estatus
        if self.estatus == self.Estatus.BLOQUEADO:
            self.is_active = False
        elif self.estatus == self.Estatus.ACTIVO:
            self.is_active = True
        
        super().save(*args, **kwargs)

    def __str__(self):
        return self.username

    def tiene_permiso(self, clave_permiso):
        """
        Verifica si el usuario tiene un permiso específico basado en sus roles asignados
        Y sus overrides (UsuarioPermiso).
        
        Prioridad:
        1. Superuser/Admin Empresa -> True
        2. Override DENY -> False
        3. Roles -> True
        4. Override GRANT -> True
        """
        # 1. Superuser global siempre tiene acceso
        if self.is_superuser:
            return True

        # 2. Admin de empresa tiene acceso total a su empresa
        if self.is_admin_empresa:
            return True

        # 3. Revisar Overrides (UsuarioPermiso)
        # Consultamos si existe algún override para este permiso
        qs_overrides = self.overrides_permisos.filter(permiso__clave=clave_permiso)
        
        # Si existe un DENY explícito, denegar acceso inmediatamente
        if qs_overrides.filter(tipo="deny").exists():
            return False

        # 4. Verificar roles asignados
        tiene_por_rol = self.asignaciones_roles.filter(
            rol__estatus="activo",
            rol__permisos__clave=clave_permiso
        ).exists()
        
        if tiene_por_rol:
            return True
            
        # 5. Si no tiene por rol, verificar si tiene un GRANT explícito
        return qs_overrides.filter(tipo="grant").exists()
