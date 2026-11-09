"""Utilidades comunes de los endpoints ``.../kpis/``.

Un solo lugar para lo que todos repetían: rechazar parámetros desconocidos
(en vez de responder 200 como si se hubieran aplicado), leer fechas y marcar
cuándo un ``drill_down`` está truncado.
"""

from rest_framework.exceptions import ValidationError
from django.utils.dateparse import parse_date
from rest_framework.settings import api_settings

TOPE_DRILL_DOWN = 20
# Parámetros que interpreta DRF mismo, no la vista.
PARAMETROS_DRF = frozenset(p for p in (api_settings.URL_FORMAT_OVERRIDE,) if p)


def validar_parametros(request, permitidos):
    """400 si el query string trae parámetros que el endpoint no soporta. Los de DRF
    (``?format=``, ``URL_FORMAT_OVERRIDE``) no son filtros y siempre pasan (EC-782):
    los listados los aceptan y estos endpoints respondían 400."""
    extra = sorted(set(request.query_params) - set(permitidos) - PARAMETROS_DRF)
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


# Lector único de enteros del query string (EC-829). ``int()`` acepta ``1_0`` (10),
# ``+5``, dígitos no ASCII (``٣``, ``５``) y espacios Unicode alrededor: un valor
# así filtraba otro registro en vez de dar 400.
ESPACIOS_ASCII = " \t\n\r\x0b\x0c"
ENTERO_MAXIMO = 2**63 - 1  # bigint de Postgres
DIGITOS_MAXIMOS = 19
MENSAJE_ENTERO = "Debe ser un entero válido."
MENSAJE_POSITIVO = "Debe ser un entero mayor a 0."


def entero_estricto(valor, nombre, *, positivo=False, con_signo=False):
    """Entero de un valor del query string; vacío → ``None``; inválido → 400 con
    la clave ``nombre``.

    Solo dígitos ASCII (hasta 19, dentro de bigint), con los espacios ASCII de
    alrededor recortados. ``con_signo`` admite un ``-`` inicial (nunca ``+``);
    ``positivo`` rechaza 0 y negativos con ``MENSAJE_POSITIVO``.
    """
    texto = (valor or "").strip(ESPACIOS_ASCII)
    if not texto:
        return None
    negativo = texto.startswith("-")
    digitos = texto[1:] if negativo else texto
    if not (digitos.isascii() and digitos.isdigit()) or len(digitos) > DIGITOS_MAXIMOS:
        raise ValidationError({nombre: MENSAJE_ENTERO})
    if negativo and not con_signo:
        raise ValidationError({nombre: MENSAJE_POSITIVO if positivo else MENSAJE_ENTERO})
    numero = int(texto)
    if not -ENTERO_MAXIMO - 1 <= numero <= ENTERO_MAXIMO:
        raise ValidationError({nombre: MENSAJE_ENTERO})
    if positivo and numero <= 0:
        raise ValidationError({nombre: MENSAJE_POSITIVO})
    return numero


def entero_param(request, nombre, *, positivo=False, con_signo=False):
    """Entero del query param ``nombre`` (``entero_estricto``); vacío → ``None``."""
    return entero_estricto(request.query_params.get(nombre), nombre, positivo=positivo, con_signo=con_signo)


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
