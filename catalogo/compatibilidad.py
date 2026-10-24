"""Validación de la matriz prenda/proceso (``CategoriaProductoProceso``, EC-572)."""

from rest_framework.exceptions import ValidationError

from catalogo.models import CategoriaProductoProceso

Proceso = CategoriaProductoProceso.Proceso


def _norm(valor):
    return str(valor).strip().lower() if valor not in (None, "") else ""


def _dicts(cfg):
    if isinstance(cfg, dict):
        return [cfg]
    if isinstance(cfg, list):
        return [c for c in cfg if isinstance(c, dict)]
    return []


def ubicaciones_usadas(proceso, cfg):
    """Ubicaciones (cada una, el conjunto de nombres/códigos que la identifican) del ``*_config``."""
    usadas = []
    for c in _dicts(cfg):
        if proceso == Proceso.BORDADO:
            for u in c.get("ubicaciones") or []:
                if isinstance(u, dict):
                    usadas.append({_norm(u.get("codigo")), _norm(u.get("nombre"))} - {""})
        if c.get("posicion"):
            usadas.append({_norm(c["posicion"])})
    return [u for u in usadas if u]


def _procesos_de(talla):
    if talla.lleva_bordado:
        yield Proceso.BORDADO, talla.bordado_config
    if talla.lleva_reflejante:
        yield Proceso.REFLEJANTE, talla.reflejante_config
    if talla.lleva_corte_manga:
        yield Proceso.CORTE_MANGA, talla.corte_manga_config


def errores_compatibilidad(empresa, tallas):
    """Lista de errores de las ``tallas`` (de cotización o pedido) contra la matriz.

    ``tallas``: iterable con ``select_related`` a ``<detalle>__producto__categoria_producto``
    y el detalle accesible como ``cotizacion_detalle`` o ``pedido_detalle``.
    """
    reglas = {}
    for fila in CategoriaProductoProceso.objects.filter(empresa=empresa, activo=True):
        reglas.setdefault(fila.proceso, {}).setdefault(fila.categoria_producto_id, set()).add(_norm(fila.ubicacion))
    if not reglas:
        return []

    errores = []
    vistos = set()
    for talla in tallas:
        detalle = getattr(talla, "cotizacion_detalle", None) or getattr(talla, "pedido_detalle", None)
        producto = getattr(detalle, "producto", None)
        categoria = getattr(producto, "categoria_producto", None)
        if categoria is None:
            continue
        for proceso, cfg in _procesos_de(talla):
            por_categoria = reglas.get(proceso)
            if por_categoria is None:
                continue
            permitidas = por_categoria.get(categoria.pk)
            etiqueta = Proceso(proceso).label
            if permitidas is None:
                msg = f"La categoría «{categoria.nombre}» no admite {etiqueta}."
            elif "" in permitidas:
                continue
            else:
                msg = ""
                for ubic in ubicaciones_usadas(proceso, cfg):
                    if not ubic & permitidas:
                        msg = (
                            f"La categoría «{categoria.nombre}» no admite {etiqueta} en "
                            f"«{sorted(ubic)[0]}»."
                        )
                        break
                if not msg:
                    continue
            if msg not in vistos:
                vistos.add(msg)
                errores.append(msg)
    return errores


def exigir_compatibilidad(empresa, tallas):
    """Lanza 400 si alguna talla viola la matriz. ``tallas`` puede ser un queryset (se le hace
    ``select_related`` hasta la categoría) o una lista."""
    if hasattr(tallas, "select_related"):
        detalle = "cotizacion_detalle" if hasattr(tallas.model, "cotizacion_detalle") else "pedido_detalle"
        tallas = tallas.select_related(f"{detalle}__producto__categoria_producto")
    errores = errores_compatibilidad(empresa, tallas)
    if errores:
        raise ValidationError({"compatibilidad_proceso": errores})
