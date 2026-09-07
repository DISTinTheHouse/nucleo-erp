import re


CLAVE_DEPARTAMENTO_MESA_CONTROL = "MESACONTROL"
CLAVE_DEPARTAMENTO_VENTAS = "VENTAS"
CLAVE_DEPARTAMENTO_CONTA_VENTAS = "CONTAVENTAS"
CLAVE_DEPARTAMENTO_MESA_CONTROL_Y_VENTAS = "MESACONTROLYVENTAS"
CLAVE_DEPARTAMENTO_COMPRAS = "COMPRAS"
CLAVE_DEPARTAMENTO_CONTABILIDAD = "CONTABILIDAD"
CLAVE_DEPARTAMENTO_CONTA_COMPRAS = "CONTACOMPRAS"
CLAVE_DEPARTAMENTO_RH = "RH"
CLAVE_DEPARTAMENTO_PRODUCCION = "PRODUCCION"
CLAVE_DEPARTAMENTO_WMS = "WMS"

CLAVES_CONTABILIDAD_VENTAS = frozenset(
    {
        CLAVE_DEPARTAMENTO_MESA_CONTROL,
        CLAVE_DEPARTAMENTO_VENTAS,
        CLAVE_DEPARTAMENTO_CONTA_VENTAS,
        CLAVE_DEPARTAMENTO_MESA_CONTROL_Y_VENTAS,
    }
)

CLAVES_CONTABILIDAD_COMPRAS = frozenset(
    CLAVES_CONTABILIDAD_VENTAS
    | {
        CLAVE_DEPARTAMENTO_COMPRAS,
        CLAVE_DEPARTAMENTO_CONTABILIDAD,
        CLAVE_DEPARTAMENTO_CONTA_COMPRAS,
    }
)

_TOKEN_EQUIVALENCIAS = {
    "MESACONTROL": CLAVE_DEPARTAMENTO_MESA_CONTROL,
    "MESADECONTROL": CLAVE_DEPARTAMENTO_MESA_CONTROL,
    "MESACONTROLYVENTAS": CLAVE_DEPARTAMENTO_MESA_CONTROL_Y_VENTAS,
    "CONTAVENTAS": CLAVE_DEPARTAMENTO_CONTA_VENTAS,
    "CONTACOMPRAS": CLAVE_DEPARTAMENTO_CONTA_COMPRAS,
    "VENTAS": CLAVE_DEPARTAMENTO_VENTAS,
    "COMPRAS": CLAVE_DEPARTAMENTO_COMPRAS,
    "CONTABILIDAD": CLAVE_DEPARTAMENTO_CONTABILIDAD,
    "CONTABILID": CLAVE_DEPARTAMENTO_CONTABILIDAD,
    "RH": CLAVE_DEPARTAMENTO_RH,
    "PRODUCCION": CLAVE_DEPARTAMENTO_PRODUCCION,
    "WMS": CLAVE_DEPARTAMENTO_WMS,
    "ALMACENMP": CLAVE_DEPARTAMENTO_WMS,
}

_TOKEN_PREFIJOS = (
    ("MESACONTROLYVENTAS", CLAVE_DEPARTAMENTO_MESA_CONTROL_Y_VENTAS),
    ("MESACONTROL", CLAVE_DEPARTAMENTO_MESA_CONTROL),
    ("MESADECONTROL", CLAVE_DEPARTAMENTO_MESA_CONTROL),
    ("CONTAVENTAS", CLAVE_DEPARTAMENTO_CONTA_VENTAS),
    ("CONTACOMPRAS", CLAVE_DEPARTAMENTO_CONTA_COMPRAS),
    ("CONTABILIDAD", CLAVE_DEPARTAMENTO_CONTABILIDAD),
    ("CONTABILID", CLAVE_DEPARTAMENTO_CONTABILIDAD),
    ("COMPRAS", CLAVE_DEPARTAMENTO_COMPRAS),
    ("PRODUCCION", CLAVE_DEPARTAMENTO_PRODUCCION),
    ("ALMACENMP", CLAVE_DEPARTAMENTO_WMS),
    ("VENTAS", CLAVE_DEPARTAMENTO_VENTAS),
    ("WMS", CLAVE_DEPARTAMENTO_WMS),
    ("RH", CLAVE_DEPARTAMENTO_RH),
)


def normalizar_token_rol(value) -> str:
    try:
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    except Exception:
        return ""


def _mapear_token_departamento(token: str) -> str:
    if not token:
        return ""
    mapped = _TOKEN_EQUIVALENCIAS.get(token)
    if mapped:
        return mapped
    for prefijo, clave in _TOKEN_PREFIJOS:
        if token.startswith(prefijo):
            return clave
    return ""


def canonicalizar_clave_departamento(value) -> str:
    token = normalizar_token_rol(value)
    if not token:
        return ""
    return _mapear_token_departamento(token) or token


def inferir_clave_departamento(*values) -> str:
    for value in values:
        mapped = _mapear_token_departamento(normalizar_token_rol(value))
        if mapped:
            return mapped
    return ""


def resolver_clave_departamento_rol(rol) -> str:
    if not rol:
        return ""
    clave_departamento = canonicalizar_clave_departamento(
        getattr(rol, "clave_departamento", None)
    )
    if clave_departamento:
        return clave_departamento
    return inferir_clave_departamento(
        getattr(rol, "codigo", None),
        getattr(rol, "nombre", None),
    )


def claves_departamento_usuario(user, empresa=None) -> set[str]:
    from .models import Rol, UsuarioRol
    from usuarios.models import Usuario

    if not getattr(user, "pk", None):
        return set()
    if getattr(user, "estatus", None) != Usuario.Estatus.ACTIVO:
        return set()

    qs = UsuarioRol.objects.filter(usuario=user, rol__estatus=Rol.Estatus.ACTIVO)
    empresa_id = getattr(empresa, "pk", empresa)
    if empresa_id is None:
        empresa_id = getattr(user, "empresa_id", None)
    if empresa_id is None:
        return set()
    qs = qs.filter(rol__empresa_id=empresa_id)

    claves = set()
    for asignacion in qs.select_related("rol"):
        clave = resolver_clave_departamento_rol(getattr(asignacion, "rol", None))
        if clave:
            claves.add(clave)
    return claves


def usuario_tiene_clave_departamento(user, clave_departamento, empresa=None) -> bool:
    clave = canonicalizar_clave_departamento(clave_departamento)
    if not clave:
        return False
    return clave in claves_departamento_usuario(user, empresa=empresa)


def usuarios_ids_por_clave_departamento(
    empresa, clave_departamento, solo_usuarios_activos=True
) -> list[int]:
    from .models import Rol, UsuarioRol
    from usuarios.models import Usuario

    clave = canonicalizar_clave_departamento(clave_departamento)
    if not clave:
        return []

    qs = UsuarioRol.objects.filter(
        rol__empresa=empresa,
        rol__estatus=Rol.Estatus.ACTIVO,
    )
    if solo_usuarios_activos:
        qs = qs.filter(usuario__estatus=Usuario.Estatus.ACTIVO)

    usuario_ids = []
    vistos = set()
    for asignacion in qs.select_related("rol"):
        if resolver_clave_departamento_rol(getattr(asignacion, "rol", None)) != clave:
            continue
        if asignacion.usuario_id in vistos:
            continue
        vistos.add(asignacion.usuario_id)
        usuario_ids.append(asignacion.usuario_id)
    return usuario_ids
