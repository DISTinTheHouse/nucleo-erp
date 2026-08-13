from __future__ import annotations

import copy
import logging
import re

logger = logging.getLogger(__name__)

# Campos monetarios / de contabilidad que se ocultan a roles no autorizados.
# Por ahora OrdenBordado no tiene $ propios; lista vacía para mantener el
# patrón escalable: cuando se agreguen costo_unitario / importe / precio se
# edita aquí y el filtro los aplica sin tocar la vista.
CAMPOS_CONTABILIDAD_BORDADO = []
CAMPOS_CONTABILIDAD_BORDADO_DETALLE = []
CAMPOS_CONTABILIDAD_BORDADO_AVANCE = []
CAMPOS_CONTABILIDAD_BORDADO_INCIDENCIA = []

TOKENS_ROL_VER_TODO = {"MESACONTROL", "VENTAS", "CONTAVENTAS", "MESACONTROLYVENTAS"}


def _normalizar_rol_name(s) -> str:
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
            tok = _normalizar_rol_name(getattr(rol, attr, None))
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


def filtrar_campos_contabilidad_orden_bordado(data, user):
    """Oculta los campos $$ de OrdenBordado a roles no autorizados.

    Si el usuario puede ver contabilidad, devuelve ``data`` sin mutarla.
    De lo contrario, remueve los campos declarados en las listas de arriba
    operando sobre una copia profunda para no tocar ``serializer.data``.
    """
    if puede_ver_contabilidad(user):
        return data
    try:
        cleaned = copy.deepcopy(data)
    except Exception:
        logger.warning("No se pudo copiar serializer.data en filtro contabilidad OB")
        return data

    _drop_keys(cleaned, CAMPOS_CONTABILIDAD_BORDADO)

    for det in cleaned.get("detalles") or []:
        _drop_keys(det, CAMPOS_CONTABILIDAD_BORDADO_DETALLE)

    for av in cleaned.get("avances") or []:
        _drop_keys(av, CAMPOS_CONTABILIDAD_BORDADO_AVANCE)

    for inc in cleaned.get("incidencias") or []:
        _drop_keys(inc, CAMPOS_CONTABILIDAD_BORDADO_INCIDENCIA)

    return cleaned


def armar_pedido_vinculado(orden):
    """``{id, folio}`` del pedido madre, en un objeto dedicado.

    Se prefiere un objeto ``pedido_vinculado`` a seguir expandiendo campos
    planos (``pedido_id`` / ``pedido_folio``), para que cuando el FE necesite
    más datos después (ej. cliente) la forma de extensión es agregar keys
    dentro de este dict, sin romper el contrato ni crear prefijos nuevos.
    """
    pedido = getattr(orden, "pedido", None)
    if not pedido:
        return None
    return {"id": pedido.pk, "folio": str(getattr(pedido, "folio", pedido.pk) or pedido.pk)}
