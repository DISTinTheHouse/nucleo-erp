from django.db import models

class StatusChoices(models.TextChoices):
    ACTIVE = "active", "Activo"
    DELETED = "deleted", "Eliminado"
    ARCHIVED = "archived", "Archivado"