"""Piezas comunes de las opciones de filtros de encabezado en modo servidor:
meses ``AAAA-MM`` (parámetro y opciones con etiqueta en español) y el orden
alfabético que imita al ``localeCompare(..., "es")`` del front.

Las comparten ``existencias/por-almacen/`` (EC-774) y el listado de pedidos
(EC-782); antes cada uno tenía su copia.
"""

import re
import unicodedata

from rest_framework.exceptions import ValidationError

MES_RE = re.compile(r"^(\d{4})-(\d{2})$")
MESES = (
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
)


def clave_orden(texto):
    """Orden alfabético aproximado al ``localeCompare(..., "es")`` del front."""
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return (sin_acentos.casefold(), texto)


def mes_param(request, nombre, permitir=()):
    """``(año, mes)`` de un ``AAAA-MM`` del query string; vacío → ``None``; un valor
    de ``permitir`` se devuelve tal cual; otro formato o mes fuera de 1–12 → 400
    ``Formato AAAA-MM.`` (más los valores permitidos, si hay)."""
    valor = (request.query_params.get(nombre) or "").strip()
    if not valor:
        return None
    if valor in permitir:
        return valor
    coincide = MES_RE.match(valor)
    if not coincide or not 1 <= int(coincide.group(2)) <= 12:
        extra = f" o {', '.join(permitir)}" if permitir else ""
        raise ValidationError({nombre: f"Formato AAAA-MM{extra}."})
    return int(coincide.group(1)), int(coincide.group(2))


def opciones_meses(fechas):
    """``[{value: "AAAA-MM", label: "Mes AAAA"}]``, más reciente primero."""
    return [
        {"value": f"{m.year:04d}-{m.month:02d}", "label": f"{MESES[m.month - 1]} {m.year}"}
        for m in sorted(fechas or [], reverse=True)
    ]
