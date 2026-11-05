from django.apps import AppConfig


class SeguridadConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'seguridad'

    def ready(self):
        # Caché de claves de departamento por petición (``role_identity``): se abre al
        # empezar cada petición y se cierra al terminar.
        from django.core.signals import request_finished, request_started

        from .role_identity import abrir_cache_claves_departamento, cerrar_cache_claves_departamento

        request_started.connect(abrir_cache_claves_departamento, dispatch_uid="claves-departamento-abrir")
        request_finished.connect(cerrar_cache_claves_departamento, dispatch_uid="claves-departamento-cerrar")
