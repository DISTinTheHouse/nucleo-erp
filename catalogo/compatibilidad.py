"""Validación de la matriz prenda/proceso (``CategoriaProductoProceso``, EC-572 / EC-575).

Regla por categoría (EC-575): una categoría con al menos una fila activa está
"configurada" y solo admite los procesos que tiene en la matriz; en cada proceso
admitido, una fila con ``ubicacion`` vacía admite cualquier ubicación y, si no,
solo las listadas (sin distinguir mayúsculas). Una categoría sin filas activas no
se restringe. En corte de manga una fila significa "lo admite".
"""

from django.db.models import Q
from rest_framework.exceptions import ValidationError

from catalogo.models import CategoriaProductoProceso

Proceso = CategoriaProductoProceso.Proceso

# Ubicación comodín en el bloque del onboarding: el proceso se admite en cualquiera.
CUALQUIER_UBICACION = "*"


def _norm(valor):
    return str(valor).strip().lower() if valor not in (None, "") else ""


def normalizar_ubicacion(valor):
    """Cómo se guarda ``ubicacion``: sin espacios en los extremos y en mayúsculas,
    igual que los códigos del front (``HOMBROS``, ``A``...)."""
    return (valor or "").strip().upper()


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


def _procesos_de(talla, procesos=None):
    for proceso, lleva, cfg in (
        (Proceso.BORDADO, "lleva_bordado", "bordado_config"),
        (Proceso.REFLEJANTE, "lleva_reflejante", "reflejante_config"),
        (Proceso.CORTE_MANGA, "lleva_corte_manga", "corte_manga_config"),
    ):
        if (procesos is None or proceso in procesos) and getattr(talla, lleva, False):
            yield proceso, getattr(talla, cfg, None)


def _categoria_de(talla):
    """Categoría con la que se evalúa la talla: la del producto; en una muestra de
    cotización (sin producto), la ``categoria_producto`` de la línea. Las muestras
    de pedido quedan fuera de la matriz (``PedidoDetalle`` no guarda categoría)."""
    detalle = getattr(talla, "cotizacion_detalle", None)
    de_cotizacion = detalle is not None
    if detalle is None:
        detalle = getattr(talla, "pedido_detalle", None)
    producto = getattr(detalle, "producto", None)
    if producto is not None:
        return getattr(producto, "categoria_producto", None)
    if de_cotizacion:
        return getattr(detalle, "categoria_producto", None)
    return None


def reglas_por_categoria(empresa_id, todas_las_empresas=False):
    """``{categoria_id: {proceso: {ubicaciones normalizadas}}}`` de las filas activas
    de la empresa, o de todas con ``todas_las_empresas`` (una consulta). ``""`` en el
    conjunto = cualquier ubicación. Cada categoría es de una sola empresa, así que la
    clave por categoría no mezcla reglas entre empresas."""
    filas = CategoriaProductoProceso.objects.filter(activo=True)
    if not todas_las_empresas:
        filas = filas.filter(empresa_id=empresa_id)
    reglas = {}
    for categoria_id, proceso, ubicacion in filas.values_list("categoria_producto_id", "proceso", "ubicacion"):
        reglas.setdefault(categoria_id, {}).setdefault(proceso, set()).add(_norm(ubicacion))
    return reglas


def matriz_onboarding(empresa_id):
    """Bloque de la matriz para el front (EC-575), por id de categoría (str).

    ``{"<categoria_id>": {"bordado": ["*"] | ["A", "C"], "reflejante": [...],
    "corte_manga": ["*"]}}``. Una categoría ausente no se restringe; una categoría
    presente sin un proceso no lo admite; ``["*"]`` = cualquier ubicación. Una consulta.
    Sin ``empresa_id`` (superusuario sin empresa, cuyo onboarding lista productos de
    todas las empresas) trae las filas activas de todas.
    """
    return {
        str(categoria_id): {
            proceso: [CUALQUIER_UBICACION] if "" in ubicaciones else sorted(u.upper() for u in ubicaciones)
            for proceso, ubicaciones in sorted(por_proceso.items())
        }
        for categoria_id, por_proceso in reglas_por_categoria(
            empresa_id, todas_las_empresas=empresa_id is None
        ).items()
    }


def errores_compatibilidad(empresa_id, tallas, procesos=None):
    """Lista de errores de las ``tallas`` (de cotización o pedido) contra la matriz.

    ``tallas``: iterable con ``select_related`` hasta la categoría y el detalle
    accesible como ``cotizacion_detalle`` o ``pedido_detalle``. ``procesos``
    (opcional) limita la revisión a esos procesos.
    """
    # Una sola consulta chica; sin filas no se toca la lista de tallas (lazy).
    reglas = reglas_por_categoria(empresa_id)
    if not reglas:
        return []

    errores = []
    vistos = set()
    for talla in tallas:
        categoria = _categoria_de(talla)
        if categoria is None or categoria.pk not in reglas:
            continue
        admitidos = reglas[categoria.pk]
        for proceso, cfg in _procesos_de(talla, procesos):
            permitidas = admitidos.get(proceso)
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
                            f"«{normalizar_ubicacion(sorted(ubic)[0])}»."
                        )
                        break
                if not msg:
                    continue
            if msg not in vistos:
                vistos.add(msg)
                errores.append(msg)
    return errores


def exigir_compatibilidad(empresa_id, tallas, procesos=None):
    """Lanza 400 si alguna talla viola la matriz. ``tallas`` puede ser un queryset (se reduce a
    las que llevan algún proceso y se trae la categoría en el mismo JOIN) o una lista."""
    if hasattr(tallas, "select_related"):
        relaciones = ["pedido_detalle__producto__categoria_producto"]
        if hasattr(tallas.model, "cotizacion_detalle"):
            relaciones = [
                "cotizacion_detalle__producto__categoria_producto",
                "cotizacion_detalle__categoria_producto",
            ]
        tallas = tallas.filter(
            Q(lleva_bordado=True) | Q(lleva_reflejante=True) | Q(lleva_corte_manga=True)
        ).select_related(*relaciones)
    errores = errores_compatibilidad(empresa_id, tallas, procesos)
    if errores:
        raise ValidationError({"compatibilidad_proceso": errores})
