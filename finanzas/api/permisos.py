"""Permisos efectivos resueltos una sola vez por petición dentro de finanzas.

``PermisoFinanzas`` los necesita para decidir el acceso y algunos serializers (las
pólizas del detalle de CxC, EC-746) para decidir qué incluir. Sin esto cada uno volvía
a llamar a ``permisos_efectivos`` y la petición pagaba sus 2 consultas dos veces.
"""

from nucleo.permisos import permisos_efectivos

_ATRIBUTO = "_finanzas_permisos_efectivos"


def permisos_de_la_peticion(request):
    """``permisos_efectivos(request.user)``, guardado en la petición de DRF."""
    permisos = getattr(request, _ATRIBUTO, None)
    if permisos is None:
        permisos = permisos_efectivos(request.user)
        setattr(request, _ATRIBUTO, permisos)
    return permisos
