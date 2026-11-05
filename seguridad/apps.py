from django.apps import AppConfig


class SeguridadConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'seguridad'

    def ready(self):
        # Invalida las claves de departamento guardadas en los objetos usuario
        # (``role_identity.claves_departamento_usuario``) al cambiar roles o asignaciones.
        # Y al empezar cada petición, para que lo guardado dure exactamente una: un
        # objeto usuario reutilizado entre peticiones (``force_authenticate``) no
        # arrastra claves de la anterior.
        from django.core.signals import request_started
        from django.db.models.signals import post_delete, post_save

        from .models import Rol, UsuarioRol
        from .role_identity import invalidar_claves_departamento

        request_started.connect(invalidar_claves_departamento, dispatch_uid="claves-departamento-peticion")
        for modelo in (Rol, UsuarioRol):
            for nombre, senal in (("save", post_save), ("delete", post_delete)):
                senal.connect(
                    invalidar_claves_departamento, sender=modelo,
                    dispatch_uid=f"claves-departamento-{modelo.__name__}-{nombre}",
                )
