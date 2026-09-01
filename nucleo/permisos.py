"""Resolución de permisos efectivos en bloque, para consultarlos muchas veces.

``Usuario.tiene_permiso(clave)`` responde por UNA clave y gasta hasta tres consultas
en hacerlo. Eso está bien para un gate puntual, pero el buscador global pregunta por
16 claves en cada petición y ``any()`` sólo corta al ACERTAR: quien falla muchas
—Compras, Mesa de Control— pagaba ~30 consultas por búsqueda, y el buscador se
dispara con cada pulsación.

``permisos_efectivos(user)`` resuelve el conjunto entero en **2 consultas** (0 para
superusuario y admin de empresa) y devuelve un objeto que responde por pertenencia en
O(1), sin volver a tocar la BD.

Quién lo usa
------------
Este módulo es el ÚNICO cálculo en bloque del repo. Lo consumen el buscador global
(``nucleo.api.search``, para decidir qué entidades ve el usuario) y la lista de
permisos efectivos que la API de login manda al frontend (``LoginAPIView`` y
``UsuarioSerializer.get_permisos``, vía ``.claves()``). Antes cada uno de esos dos
tenía su propia copia del cálculo, con la misma precedencia escrita tres veces.

Equivalencia con ``tiene_permiso()``
------------------------------------
Es una reimplementación DELIBERADA, no una refactorización: ``tiene_permiso()`` se
queda intacto como referencia canónica para preguntar por UNA clave, y este módulo
replica su precedencia sin tocarlo. Quedan por tanto dos definiciones de la misma
regla —ésta en bloque y aquélla por clave—, y lo único que impide que se separen son
las pruebas de ``nucleo/tests.py``, que comparan ambas rutas caso por caso
(superusuario y admin con y sin DENY, DENY que gana al rol, GRANT sin rol, rol
inactivo y fallo simple).

La precedencia replicada, en el mismo orden:

1. ``is_superuser`` -> tiene todo.
2. ``is_admin_empresa`` -> tiene todo.
3. Override DENY -> no lo tiene, aunque un rol se lo conceda.
4. Rol ACTIVO que lo concede -> lo tiene.
5. Override GRANT -> lo tiene.
6. Si no, no lo tiene.

Los pasos 3-5 colapsan en ``(rol ∪ grant) - deny``, que da el mismo resultado porque
el DENY gana en ambas formulaciones.

Dos detalles de fidelidad que es fácil perder de vista:

- ``UsuarioPermiso`` tiene columnas de contexto ``empresa``/``sucursal``, pero
  ``tiene_permiso()`` **no** filtra por ellas: usa todos los overrides del usuario.
  Aquí se hace igual, a propósito.
- Un ``tipo`` de override que no sea ni ``grant`` ni ``deny`` no cuenta como ninguno
  de los dos, igual que en ``tiene_permiso()``, que consulta cada uno por separado.
"""

from seguridad.models import Rol, UsuarioPermiso


class PermisosEfectivos:
    """Conjunto de claves ya resuelto. ``clave in permisos`` no consulta nada.

    ``todo=True`` representa al superusuario y al admin de empresa: contienen
    cualquier clave sin necesidad de haber leído el catálogo.
    """

    __slots__ = ("_todo", "_claves")

    def __init__(self, todo: bool, claves: frozenset):
        self._todo = todo
        self._claves = claves

    def __contains__(self, clave: str) -> bool:
        return self._todo or clave in self._claves

    def alguno(self, claves) -> bool:
        """``True`` si tiene AL MENOS UNA de ``claves``.

        Con ``claves`` vacío devuelve ``False`` incluso para superusuario: no hay
        ninguna clave que consultar. Quien reciba la lista de fuera debe validar
        que no venga vacía por descuido.
        """
        return any(clave in self for clave in claves)

    def claves(self) -> list:
        """Lista ordenada de las claves CONCRETAS que tiene el usuario.

        Para superusuario y admin de empresa devuelve ``[]``: no se leyó el
        catálogo porque lo tienen todo, y ése es justo el contrato que la API de
        login lleva publicando (el frontend los trata como "tienen todo" al ver la
        lista vacía). No confundir con "no tiene permisos": para eso está
        ``__contains__``.
        """
        return sorted(self._claves)

    def __repr__(self):  # pragma: no cover - ayuda al depurar
        if self._todo:
            return "<PermisosEfectivos: todo>"
        return f"<PermisosEfectivos: {len(self._claves)} claves>"


def permisos_efectivos(user) -> PermisosEfectivos:
    """Permisos efectivos del usuario, en 2 consultas (0 si es superusuario/admin)."""
    # Pasos 1 y 2: cortocircuito idéntico al de ``tiene_permiso()``. No se lee el
    # catálogo porque la respuesta es ``True`` para cualquier clave.
    if getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False):
        return PermisosEfectivos(todo=True, claves=frozenset())

    # Sin usuario autenticado no hay nada que resolver. ``tiene_permiso()`` no cubre
    # este caso (reventaría con ``AnonymousUser``); aquí se falla cerrado.
    if not getattr(user, "is_authenticated", False):
        return PermisosEfectivos(todo=False, claves=frozenset())

    # Consulta 1: los overrides del usuario, grants y denies de una sola pasada.
    concedidos = set()
    denegados = set()
    for tipo, clave in user.overrides_permisos.values_list("tipo", "permiso__clave"):
        if tipo == UsuarioPermiso.TIPO_DENY:
            denegados.add(clave)
        elif tipo == UsuarioPermiso.TIPO_GRANT:
            concedidos.add(clave)

    # Consulta 2: lo que conceden sus roles ACTIVOS. Se descartan los NULL que
    # produce el LEFT JOIN cuando un rol no tiene permisos asignados. Se compara
    # contra ``None`` y no por veracidad: una clave vacía es un dato absurdo, pero
    # ``tiene_permiso("")`` la encontraría, y aquí se replica su decisión, no se
    # corrige.
    concedidos.update(
        clave
        for clave in user.asignaciones_roles.filter(
            rol__estatus=Rol.Estatus.ACTIVO
        ).values_list("rol__permisos__clave", flat=True)
        if clave is not None
    )

    return PermisosEfectivos(todo=False, claves=frozenset(concedidos - denegados))
