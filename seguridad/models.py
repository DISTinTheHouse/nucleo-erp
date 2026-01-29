from django.db import models


class Permiso(models.Model):
    # Catálogo global de permisos del PRODUCTO
    clave = models.CharField(max_length=120, unique=True)  # ventas.pedidos.leer
    nombre = models.CharField(max_length=120)
    descripcion = models.TextField(blank=True, null=True)
    modulo = models.CharField(max_length=60, blank=True, null=True)  # ventas, compras, inventario, etc.

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "permisos"
        verbose_name = "Permiso"
        verbose_name_plural = "Permisos"
        indexes = [
            models.Index(fields=["modulo"]),
        ]

    def __str__(self):
        return self.clave


class Rol(models.Model):
    class Estatus(models.TextChoices):
        ACTIVO = "activo", "Activo"
        INACTIVO = "inactivo", "Inactivo"

    empresa = models.ForeignKey("nucleo.Empresa", on_delete=models.PROTECT, related_name="roles")

    codigo = models.CharField(max_length=50)  # unique por empresa
    nombre = models.CharField(max_length=120)
    descripcion = models.TextField(blank=True, null=True)

    is_system = models.BooleanField(default=False)
    estatus = models.CharField(max_length=20, choices=Estatus.choices, default=Estatus.ACTIVO)

    permisos = models.ManyToManyField(Permiso, through="RolPermiso", related_name="roles")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "roles"
        verbose_name = "Rol"
        verbose_name_plural = "Roles"
        constraints = [
            models.UniqueConstraint(fields=["empresa", "codigo"], name="uq_rol_empresa_codigo"),
        ]
        indexes = [
            models.Index(fields=["empresa", "estatus"]),
        ]

    def __str__(self):
        return f"{self.empresa.codigo} - {self.nombre}"


class UsuarioRol(models.Model):
    usuario = models.ForeignKey("usuarios.Usuario", on_delete=models.CASCADE, related_name="asignaciones_roles")
    rol = models.ForeignKey(Rol, on_delete=models.CASCADE, related_name="asignaciones_usuarios")

    # recomendado: denormalizado para validación/filtros rápidos
    empresa = models.ForeignKey("nucleo.Empresa", on_delete=models.PROTECT, related_name="usuarios_roles")

    # opcional: rol aplica a una sucursal específica
    sucursal = models.ForeignKey(
        "nucleo.Sucursal",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usuarios_roles",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "usuarios_roles"
        verbose_name = "UsuarioRol"
        verbose_name_plural = "UsuariosRoles"
        constraints = [
            models.UniqueConstraint(fields=["usuario", "rol"], name="uq_usuario_rol"),
        ]
        indexes = [
            models.Index(fields=["empresa"]),
            models.Index(fields=["usuario"]),
        ]


class RolPermiso(models.Model):
    rol = models.ForeignKey(Rol, on_delete=models.CASCADE, related_name="asignaciones_permisos")
    permiso = models.ForeignKey(Permiso, on_delete=models.CASCADE, related_name="asignaciones_roles")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "roles_permisos"
        verbose_name = "RolPermiso"
        verbose_name_plural = "RolesPermisos"
        constraints = [
            models.UniqueConstraint(fields=["rol", "permiso"], name="uq_rol_permiso"),
        ]
