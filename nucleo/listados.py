"""Piezas de los listados paginados por servidor (#863): paginación opcional,
``?ordering=`` con desempate estable y los filtros comunes de Compras."""
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.pagination import LimitOffsetPagination

from nucleo.kpis import entero_param, rango_fechas


class PaginacionOpcionalLimitOffset(LimitOffsetPagination):
    """Sin ``?limit=`` regresa la lista completa, como antes; con él,
    ``{count, next, previous, results}``. ``limit`` se topa en ``max_limit``.

    ``limit`` que no es entero positivo u ``offset`` negativo o no entero → 400
    (#950). DRF los trata como ausentes: el cliente pedía una página y recibía
    la lista completa, sin tope y con otra forma (arreglo en vez de
    ``{count, results}``). Vacíos cuentan como ausentes, igual que en
    ``entero_param``."""

    max_limit = 500

    def get_limit(self, request):
        valor = (request.query_params.get(self.limit_query_param) or "").strip()
        if not valor:
            return self.default_limit
        try:
            limit = int(valor)
        except ValueError:
            limit = 0
        if limit <= 0:
            raise ValidationError({self.limit_query_param: "Debe ser un entero positivo."})
        return min(limit, self.max_limit) if self.max_limit else limit

    def get_offset(self, request):
        valor = (request.query_params.get(self.offset_query_param) or "").strip()
        if not valor:
            return 0
        try:
            offset = int(valor)
        except ValueError:
            offset = -1
        if offset < 0:
            raise ValidationError({self.offset_query_param: "Debe ser un entero mayor o igual a 0."})
        return offset


class OrdenamientoEstableFilter(OrderingFilter):
    """``?ordering=`` con ``id`` como último desempate: sin él, las filas con el
    mismo valor se repiten o se pierden entre páginas con ``offset``. El
    desempate sigue la dirección del primer campo."""

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if ordering and not any(campo.lstrip("-") in ("id", "pk") for campo in ordering):
            ordering = [*ordering, "-id" if ordering[0].startswith("-") else "id"]
        return ordering


def filtrar_listado_compras(request, qs, estatus_validos, campo_fecha):
    """``?estatus``, ``?proveedor``, ``?sucursal`` y ``?fecha_desde``/``?fecha_hasta``
    (sobre ``campo_fecha``) de los listados de OC y recepciones. Inválidos → 400."""
    estatus = entero_param(request, "estatus")
    if estatus is not None:
        if estatus not in estatus_validos:
            raise ValidationError({"estatus": "Estatus inexistente."})
        qs = qs.filter(estatus=estatus)
    for nombre in ("proveedor", "sucursal"):
        valor = entero_param(request, nombre)
        if valor is not None:
            qs = qs.filter(**{f"{nombre}_id": valor})
    fecha_desde, fecha_hasta = rango_fechas(request)
    if fecha_desde:
        qs = qs.filter(**{f"{campo_fecha}__gte": fecha_desde})
    if fecha_hasta:
        qs = qs.filter(**{f"{campo_fecha}__lte": fecha_hasta})
    return qs


def booleano_param(request, nombre, default):
    """``true``/``false`` (también ``1``/``0``) del query string; vacío → ``default``."""
    valor = (request.query_params.get(nombre) or "").strip().lower()
    if not valor:
        return default
    if valor in ("true", "1"):
        return True
    if valor in ("false", "0"):
        return False
    raise ValidationError({nombre: "Debe ser true o false."})
