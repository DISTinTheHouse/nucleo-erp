"""Piezas de los listados paginados por servidor (#863): paginación opcional,
``?ordering=`` con desempate estable, los filtros comunes de Compras y los rangos
``*_min``/``*_max``."""
from django.core.exceptions import FieldDoesNotExist
from django.db.models import F
from rest_framework import serializers
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


class OrdenamientoNullsAlFinal(OrdenamientoEstableFilter):
    """``OrdenamientoEstableFilter`` con los NULL al final en ambas direcciones
    (EC-826). PostgreSQL los pone primero en DESC: con un campo NULL-able (p. ej.
    ``created_at`` de clientes anteriores a su historial) ``-campo`` encabezaba la
    lista con filas sin valor. Sólo SQL NULL: una cadena vacía es un valor más.

    ``NULLS LAST`` sólo en los términos que pueden ser NULL: en una columna
    NOT NULL (``id``, ``nombre``…) no cambia el resultado y en DESC impide que
    PostgreSQL recorra un btree al revés (eso da ``DESC NULLS FIRST``).

    Lista blanca, desempate y orden por defecto no cambian: sin ``?ordering=``
    válido no hay términos y el queryset conserva el orden de ``get_queryset``."""

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset
        return queryset.order_by(*[self._termino(queryset.model, campo) for campo in ordering])

    @staticmethod
    def _termino(modelo, campo):
        nombre = campo.lstrip("-")
        if not _admite_null(modelo, nombre):
            return campo
        return F(nombre).desc(nulls_last=True) if campo.startswith("-") else F(nombre).asc(nulls_last=True)


def _admite_null(modelo, ruta):
    """``True`` si ``ruta`` (``campo`` o ``fk__campo``) puede valer NULL en la
    consulta: algún campo del camino es ``null=True`` (las relaciones inversas
    lo son) o es M2M (join externo). Lo que no resuelve a un campo del modelo
    (una anotación) cuenta como NULL-able: ``NULLS LAST`` nunca está de más."""
    try:
        for parte in ruta.split("__"):
            campo = modelo._meta.pk if parte == "pk" else modelo._meta.get_field(parte)
            if campo.null or campo.many_to_many:
                return True
            modelo = campo.related_model
            if modelo is None:
                break
    except FieldDoesNotExist:
        return True
    return False


def estatus_param(request, estatus_validos, nombre="estatus"):
    """``?estatus=3`` o ``?estatus=3,4`` (EC-782) → lista de enteros; vacío → ``None``.
    Un valor no entero → 400 como ``entero_param``; uno fuera de ``estatus_validos``
    → 400 ``Estatus inexistente.``, igual que cuando sólo se aceptaba uno."""
    valor = (request.query_params.get(nombre) or "").strip()
    if not valor:
        return None
    try:
        lista = [int(parte) for parte in valor.split(",") if parte.strip()]
    except ValueError:
        raise ValidationError({nombre: "Debe ser un entero válido."})
    if not lista:
        raise ValidationError({nombre: "Debe ser un entero válido."})
    if any(e not in estatus_validos for e in lista):
        raise ValidationError({nombre: "Estatus inexistente."})
    return lista


def filtrar_listado_compras(request, qs, estatus_validos, campo_fecha):
    """``?estatus`` (uno o varios separados por coma, EC-782), ``?proveedor``,
    ``?sucursal`` y ``?fecha_desde``/``?fecha_hasta`` (sobre ``campo_fecha``) de los
    listados de OC y recepciones. Inválidos → 400."""
    estatus = estatus_param(request, estatus_validos)
    if estatus is not None:
        qs = qs.filter(estatus__in=estatus)
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


def decimal_param(request, nombre, modelo, campo):
    """Decimal del query param ``nombre`` con la precisión de ``modelo.{campo}``
    (``max_digits``/``decimal_places``: el mismo ``DecimalField`` de DRF que valida
    la escritura); vacío → ``None``. Inválido o fuera de rango → 400 ``{nombre:
    "mensaje"}``, texto como ``entero_param`` y ``booleano_param`` (EC-896; antes
    lista). Sin el tope, un valor que la columna no puede representar llegaba a
    Postgres y respondía 500."""
    valor = (request.query_params.get(nombre) or "").strip()
    if not valor:
        return None
    campo_modelo = modelo._meta.get_field(campo)
    validador = serializers.DecimalField(
        max_digits=campo_modelo.max_digits, decimal_places=campo_modelo.decimal_places,
    )
    try:
        return validador.run_validation(valor)
    except ValidationError as error:
        detalle = error.detail[0] if isinstance(error.detail, list) else error.detail
        raise ValidationError({nombre: detalle})


def filtrar_rango(request, qs, campo, lector=None):
    """``?{campo}_min=`` (inclusivo) y ``?{campo}_max=`` (exclusivo) sobre ``campo``,
    como ``matchesRangeFilter`` del front; un valor NULL no cae en ningún rango.

    ``lector(request, nombre, campo)`` lee y valida cada extremo; por defecto,
    ``decimal_param`` con la precisión de ``qs.model.{campo}``. Para un campo que no
    es del modelo (una anotación) o no es decimal, pasa el tuyo. Lo usan los
    importes del listado de pedidos y los precios del catálogo."""
    if lector is None:
        def lector(request, nombre, campo):
            return decimal_param(request, nombre, qs.model, campo)
    minimo = lector(request, f"{campo}_min", campo)
    maximo = lector(request, f"{campo}_max", campo)
    if minimo is not None:
        qs = qs.filter(**{f"{campo}__gte": minimo})
    if maximo is not None:
        qs = qs.filter(**{f"{campo}__lt": maximo})
    return qs
