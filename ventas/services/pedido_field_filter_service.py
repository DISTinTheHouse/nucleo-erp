import copy
import logging
import re

logger = logging.getLogger(__name__)

CAMPOS_CONTABILIDAD_PEDIDO = [
    "anticipo_total", "anticipo_parcial", "pago_antes_embarque",
    "por_confirmar", "otra_cantidad", "monto",
    "envio", "programa_bordados", "bordado_pantalones_extras",
    "serigrafia", "reflejante",
    "flete", "seguros", "anticipo",
    "subtotal", "descuento_global", "ieps", "iva", "gran_total",
    "forma_pago", "metodo_pago", "uso_cfdi",
    "importe_sin_iva",
]
CAMPOS_CONTABILIDAD_DETALLE = [
    "precio_lista", "precio_unitario", "costo_unitario", "subtotal_linea",
]
CAMPOS_CONTABILIDAD_TALLA = ["precio_unitario", "subtotal_talla"]
CAMPOS_CONTABILIDAD_SERVICIO = ["monto"]

TOKENS_ROL_VER_TODO = {"MESACONTROL", "VENTAS", "CONTAVENTAS", "MESACONTROLYVENTAS"}


def _normalizar_token(s: str) -> str:
    try:
        return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
    except Exception:
        return ""


def _tokens_roles_usuario(user) -> set:
    tokens = set()
    try:
        asignaciones = user.asignaciones_roles.select_related("rol").all()
    except Exception:
        try:
            asignaciones = user.asignaciones_roles.all()
        except Exception:
            return tokens

    for ur in asignaciones:
        rol = getattr(ur, "rol", None)
        if not rol:
            continue
        for attr in ("codigo", "nombre", "clave_departamento"):
            tok = _normalizar_token(getattr(rol, attr, None))
            if tok:
                tokens.add(tok)
    return tokens


def puede_ver_contabilidad(user) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    if getattr(user, "is_admin_empresa", False):
        return True

    tokens = _tokens_roles_usuario(user)
    return bool(tokens & TOKENS_ROL_VER_TODO) if tokens else False


def _drop_keys(d: dict, keys: list) -> None:
    if not isinstance(d, dict):
        return
    for k in keys:
        d.pop(k, None)


def filtrar_campos_contabilidad_pedido(data, user):
    if puede_ver_contabilidad(user):
        return data

    try:
        cleaned = copy.deepcopy(data)
    except Exception:
        logger.warning("No se pudo copiar serializer.data en filtro contabilidad pedido")
        return data

    _drop_keys(cleaned, CAMPOS_CONTABILIDAD_PEDIDO)

    for det in cleaned.get("detalles") or []:
        _drop_keys(det, CAMPOS_CONTABILIDAD_DETALLE)
        for t in det.get("tallas") or []:
            _drop_keys(t, CAMPOS_CONTABILIDAD_TALLA)

    for s in cleaned.get("servicios_extras") or []:
        _drop_keys(s, CAMPOS_CONTABILIDAD_SERVICIO)

    return cleaned
