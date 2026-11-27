"""Listado de impresiones de etiquetas RFID en modo servidor (EC-989): ``?search=``,
``?status=`` y ``?ordering=``, como el listado de encuadres RFID (EC-981) y el de
pickings (EC-921). Todo corre sobre el queryset ya acotado a la empresa activa
(``EtiquetaRFIDViewSet.get_queryset``) y sólo en ``list``."""

from django.db.models import Case, CharField, F, Value, When
from django.db.models.functions import Cast, Concat, LPad
from django.db.models.lookups import GreaterThanOrEqual
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework.exceptions import ValidationError

from nucleo.listados import BusquedaSoloListadoFilter, OrdenamientoNullsAlFinal
from wms.models import EtiquetaRFIDImpresion

# ``folio`` es una property (``LAB-{pk:06d}``), no una columna: para buscarlo en SQL
# se arma la misma cadena desde ``id``. ``LPAD`` recorta lo que pasa de 6 dígitos,
# así que un ``id`` de 7 o más va tal cual, como el ``:06d`` de Python.
CAMPO_BUSQUEDA_FOLIO = "busqueda_folio"


def folio_sql(id_expr):
    """``LAB-{id:06d}`` en SQL sobre la expresión entera ``id_expr``."""
    texto = Cast(id_expr, CharField())
    return Concat(
        Value("LAB-"),
        Case(
            When(GreaterThanOrEqual(id_expr, 1_000_000), then=texto),
            default=LPad(texto, 6, Value("0")),
            output_field=CharField(),
        ),
        output_field=CharField(),
    )

# Folio, SKU, producto y variante: las columnas de texto del historial.
CAMPOS_BUSQUEDA = (
    CAMPO_BUSQUEDA_FOLIO,
    "producto_variante__sku",
    "producto__nombre",
    "producto_variante__nombre",
)

# ``?ordering=`` con el nombre de la columna del front → ruta del ORM. ``folio``
# ordena por ``id`` (crecen juntos); ``sku`` y la variante son NULL en las
# impresiones por producto y van al final en ambas direcciones. ``id`` es el
# desempate que el front agrega (``ordering=<campo>,-id``), como en pickings y EC-981.
CAMPOS_ORDEN = {
    "id": "id",
    "folio": "id",
    "sku": "producto_variante__sku",
    "producto_nombre": "producto__nombre",
    "producto_variante_nombre": "producto_variante__nombre",
    "cantidad": "cantidad",
    "status": "status",
    "created_at": "created_at",
}

PARAMETRO_STATUS_OPENAPI = OpenApiParameter("status", OpenApiTypes.STR, description=(
    "Estatus de la impresión: `PENDIENTE`, `EXITO` o `FALLIDO`; varios separados por coma. "
    "Otro valor → 400."
))


class BusquedaEtiquetasRFIDFilter(BusquedaSoloListadoFilter):
    """``?search=`` del listado; anota ``CAMPO_BUSQUEDA_FOLIO`` sólo cuando hay
    búsqueda (``SearchFilter`` acepta anotaciones en ``search_fields``)."""

    def filter_queryset(self, request, queryset, view):
        if self.get_search_fields(view, request) and self.get_search_terms(request):
            queryset = queryset.annotate(**{CAMPO_BUSQUEDA_FOLIO: folio_sql(F("id"))})
        return super().filter_queryset(request, queryset, view)


class OrdenamientoEtiquetasRFIDFilter(OrdenamientoNullsAlFinal):
    """``?ordering=`` con la lista blanca ``CAMPOS_ORDEN`` (nombres de columna del
    front), ``id`` de desempate y NULL al final. Sin ``?ordering=`` válido queda el
    orden de ``get_queryset`` (``-created_at``, ``-id``)."""

    def get_ordering(self, request, queryset, view):
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering
        rutas = []
        for campo in ordering:
            signo = "-" if campo.startswith("-") else ""
            ruta = f"{signo}{CAMPOS_ORDEN.get(campo.lstrip('-'), campo.lstrip('-'))}"
            if ruta.lstrip("-") not in {r.lstrip("-") for r in rutas}:
                rutas.append(ruta)
        return rutas


def filtrar_status(request, queryset):
    """``?status=EXITO`` o ``?status=PENDIENTE,FALLIDO``; vacío, sin filtro. Un valor
    fuera de ``EtiquetaRFIDImpresion.Estatus`` → 400, como ``?estatus=`` de EC-981."""
    valor = (request.query_params.get("status") or "").strip()
    if not valor:
        return queryset
    lista = [parte.strip().upper() for parte in valor.split(",") if parte.strip()]
    if not lista or any(e not in EtiquetaRFIDImpresion.Estatus.values for e in lista):
        raise ValidationError({"status": "Estatus inexistente. Usa PENDIENTE, EXITO o FALLIDO."})
    return queryset.filter(status__in=lista)
