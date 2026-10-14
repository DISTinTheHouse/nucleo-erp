"""Utilidades comunes de los endpoints ``.../kpis/``.

Un solo lugar para lo que todos repetían: rechazar parámetros desconocidos
(en vez de responder 200 como si se hubieran aplicado), leer fechas y marcar
cuándo un ``drill_down`` está truncado.
"""

from rest_framework.exceptions import ValidationError
from django.utils.dateparse import parse_date

TOPE_DRILL_DOWN = 20


def validar_parametros(request, permitidos):
    """400 si el query string trae parámetros que el endpoint no soporta."""
    extra = sorted(set(request.query_params) - set(permitidos))
    if extra:
        raise ValidationError({
            "parametros": (
                f"Parámetros no soportados: {', '.join(extra)}. "
                f"Soportados: {', '.join(sorted(permitidos)) or 'ninguno'}."
            )
        })


def fecha_param(request, nombre):
    """Fecha ``YYYY-MM-DD`` del query string; vacía → ``None``; inválida → 400."""
    valor = (request.query_params.get(nombre) or "").strip()
    if not valor:
        return None
    try:
        fecha = parse_date(valor)
    except ValueError:
        fecha = None
    if fecha is None:
        raise ValidationError({nombre: "Fecha inválida; formato YYYY-MM-DD."})
    return fecha


def rango_fechas(request, desde="fecha_desde", hasta="fecha_hasta"):
    fecha_desde = fecha_param(request, desde)
    fecha_hasta = fecha_param(request, hasta)
    if fecha_desde and fecha_hasta and fecha_desde > fecha_hasta:
        raise ValidationError({desde: f"No puede ser posterior a `{hasta}`."})
    return fecha_desde, fecha_hasta


def entero_param(request, nombre):
    """Entero del query string; vacío → ``None``; inválido → 400."""
    valor = (request.query_params.get(nombre) or "").strip()
    if not valor:
        return None
    try:
        return int(valor)
    except ValueError:
        raise ValidationError({nombre: "Debe ser un entero válido."})


def con_tope(queryset_o_lista, tope=TOPE_DRILL_DOWN):
    """``(filas, total, truncado)`` para un ``drill_down`` acotado a ``tope``.

    Acepta un queryset (hace un ``count()`` aparte) o una lista ya armada.
    """
    if isinstance(queryset_o_lista, list):
        total = len(queryset_o_lista)
        filas = queryset_o_lista[:tope]
    else:
        total = queryset_o_lista.count()
        filas = list(queryset_o_lista[:tope])
    return filas, total, total > len(filas)


def marcar_tope(bloque, total, filas, clave="drill_down"):
    """Agrega ``<clave>_total`` y ``<clave>_truncado`` al bloque del KPI."""
    bloque[f"{clave}_total"] = total
    bloque[f"{clave}_truncado"] = total > len(filas)
    return bloque
