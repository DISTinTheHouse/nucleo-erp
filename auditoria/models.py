from django.db import models

# class AuditoriaEvento(models.Model):
#     id_evento = models.BigAutoField(primary_key=True)
#
#     empresa = models.ForeignKey("nucleo.Empresa", on_delete=models.PROTECT, related_name="auditorias")
#     usuario = models.ForeignKey(
#         "usuarios.Usuario",
#         on_delete=models.SET_NULL,
#         null=True,
#         blank=True,
#         related_name="auditorias",
#     )
#
#     modulo = models.CharField(max_length=60)  # inventarios, ventas, etc.
#     accion = models.CharField(max_length=30)  # CREATE/UPDATE/DELETE/LOGIN/EXPORT
#     tabla = models.CharField(max_length=80, blank=True, null=True)
#     id_registro = models.CharField(max_length=80, blank=True, null=True)
#
#     antes_json = models.JSONField(blank=True, null=True)
#     despues_json = models.JSONField(blank=True, null=True)
#
#     ip = models.GenericIPAddressField(blank=True, null=True)
#     user_agent = models.TextField(blank=True, null=True)
#
#     created_at = models.DateTimeField(auto_now_add=True)
#
#     class Meta:
#         db_table = "auditoria_eventos"
#         verbose_name = "Auditoría"
#         verbose_name_plural = "Auditorías"
#         indexes = [
#             models.Index(fields=["empresa", "created_at"]),
#             models.Index(fields=["modulo", "accion"]),
#         ]
#
#     def __str__(self):
#         return f"{self.empresa.codigo} {self.modulo}:{self.accion}"
