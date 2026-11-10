"""Pertenencia del alcance de un usuario a su empresa (EC-844).

Un solo lugar para "sucursal por defecto, sucursales y departamentos son de la
empresa del usuario": lo usa ``UsuarioSerializer.validate``, que es el camino
de ``/api/v1/usuarios/`` y de la herramienta ``create_usuario`` del asistente.
"""

MENSAJES = {
    "sucursal_default": "La sucursal seleccionada no pertenece a la empresa del usuario.",
    "sucursales": "Una o más sucursales no pertenecen a la empresa del usuario.",
    "departamentos": "Uno o más departamentos no pertenecen a la empresa del usuario.",
}


def errores_de_alcance(empresa_id, *, sucursal_default=None, sucursales=None, departamentos=None):
    """``{campo: mensaje}`` con lo que no pertenece a la empresa ``empresa_id``;
    vacío si todo cuadra.

    Solo revisa lo que llega (``None`` = no se envió). Sin empresa, cualquier
    sucursal o departamento es ajeno. Compara ``empresa_id`` de las instancias
    ya resueltas por el serializer, sin consultas extra.
    """

    def ajenos(objetos):
        return any(obj.empresa_id != empresa_id or empresa_id is None for obj in objetos)

    errores = {}
    if sucursal_default is not None and ajenos([sucursal_default]):
        errores["sucursal_default"] = MENSAJES["sucursal_default"]
    if sucursales and ajenos(sucursales):
        errores["sucursales"] = MENSAJES["sucursales"]
    if departamentos and ajenos(departamentos):
        errores["departamentos"] = MENSAJES["departamentos"]
    return errores
