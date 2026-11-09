"""Permisos efectivos resueltos una sola vez por petición dentro de finanzas.

``PermisoFinanzas`` los necesita para decidir el acceso y algunos serializers (las
pólizas del detalle de CxC, EC-746) para decidir qué incluir. Sin esto cada uno volvía
a llamar a ``permisos_efectivos`` y la petición pagaba sus 2 consultas dos veces.
"""

# Vive en ``nucleo.permisos`` desde #1020: una sola caché por petición para todo
# el proyecto. Se reexporta para los imports existentes.
from nucleo.permisos import permisos_de_la_peticion  # noqa: F401
