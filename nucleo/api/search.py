"""Buscador global federado: ``GET /api/v1/search/?q=``.

Consulta varias entidades a la vez y devuelve los resultados **agrupados por tipo**,
recortando cada grupo a los primeros ``limit``. Está pensado para el buscador del
header: filas ligeras para pintar un dropdown, no vistas de detalle.

Vive en ``nucleo`` porque es infraestructura transversal —cruza ventas y terceros—,
igual que el resto de lo que comparten las apps (middleware, mixins, catálogos). La
ruta se declara en ``nucleo/urls.py``, junto a las demás APIView de nucleo.

Añadir una entidad
------------------
Se añade una ``EntidadBuscable`` a ``REGISTRO``. Nada más: el resto del endpoint es
genérico. Cada unidad declara de dónde salen sus filas (``alcance``), qué campos son
CÓDIGO y cuáles NOMBRE, qué columnas hacen falta, cómo ordenar y cómo serializar.

Aislamiento multi-tenant
------------------------
``alcance`` NO reimplementa nada: compone el queryset base y el predicado reales que
ya usa el ViewSet de esa entidad (``ventas.scope`` / ``terceros.scope``). La política
de superusuario es, por lo tanto, exactamente la que cada entidad ya tenía; este
endpoint no inventa una.

Permisos
--------
``IsAuthenticated`` + aislamiento, igual que el resto de ``/api/v1/``. Este backend
no valida RBAC en ningún endpoint (``Usuario.tiene_permiso()`` no tiene llamadores),
así que el buscador tampoco introduce una compuerta propia.

Estrategia de coincidencia
--------------------------
- Campos CÓDIGO (``folio``): ``istartswith`` — prefijo. Es lo que se espera al teclear
  un folio, y el btree de ``Pedido.folio`` (``db_index=True``) cubre el caso.
- Campos NOMBRE: ``icontains`` — subcadena, acelerada por los índices GIN
  ``gin_trgm_ops`` creados para esto. Se eligió ``icontains`` sobre
  ``TrigramSimilarity`` porque busca subcadena (lo que quiere un dropdown: "acme"
  debe encontrar "Comercial Acme SA"), porque es la forma que ya usa el filtro
  ``?q=`` de cotizaciones, y porque ordenar por ``similarity()`` obliga a recorrer
  toda la tabla para rankear —el índice GIN acelera el filtro, no el ORDER BY—.

Los resultados NO vienen rankeados por relevancia: cada grupo sale en el orden
natural de su entidad (el más reciente primero, o alfabético en clientes). Rankear
es el siguiente paso, no éste.
"""

from dataclasses import dataclass
from typing import Any, Callable

from django.db.models import F, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from nucleo.utils import entero_acotado
from terceros.scope import clientes_base, clientes_visibles
from ventas.scope import (
    cotizaciones_base,
    cotizaciones_visibles,
    pedidos_base,
    pedidos_visibles,
)

#: Por debajo de esto no se consulta nada y se devuelven los grupos vacíos (200).
LONGITUD_MINIMA_Q = 2

#: pg_trgm parte el texto en grupos de 3 caracteres: con menos de 3 no hay trigrama
#: que buscar y el planner cae a Seq Scan (verificado con EXPLAIN contra la BD real:
#: ``ILIKE '%acme%'`` usa ``Bitmap Index Scan``, ``ILIKE '%a%'`` no). Por eso, por
#: debajo de este umbral se consultan SÓLO los campos CÓDIGO, que van por btree y sí
#: funcionan con prefijos cortos. Así el buscador nunca emite una consulta que el
#: índice no pueda servir. Se expone en la respuesta: sin él, el frontend no puede
#: distinguir "escribe más" de "no hay resultados".
LONGITUD_MINIMA_NOMBRE = 3

LIMITE_POR_DEFECTO = 5
LIMITE_MAXIMO = 25

#: ``created_at`` es NULL-able en Pedido y en Cotizacion (se añadió después, y las
#: filas previas siguen en NULL). En PostgreSQL un DESC pone los NULL PRIMERO, así
#: que sin ``nulls_last`` el buscador encabezaría cada grupo con las filas legacy sin
#: fecha. Mismo criterio que ``PedidoViewSet.get_queryset()`` y
#: ``CotizacionViewSet._apply_filters()``.
ORDEN_RECIENTE = (F("created_at").desc(nulls_last=True), "-id")


@dataclass(frozen=True)
class EntidadBuscable:
    """Una unidad del buscador federado.

    ``alcance`` recibe el usuario autenticado y devuelve el queryset YA aislado por
    tenant; es el único punto que toca el aislamiento y siempre delega en el
    predicado real de la entidad.

    ``campos_only`` son las columnas que ``fila()`` necesita. Se aplican con
    ``.only()`` para no arrastrar filas completas —``Pedido`` tiene ~70 columnas,
    varias de ellas ``TextField``— en un endpoint que se dispara por pulsación.
    """

    tipo: str
    etiqueta: str
    alcance: Callable[[Any], Any]
    fila: Callable[[Any], dict]
    campos_codigo: tuple[str, ...] = ()
    campos_nombre: tuple[str, ...] = ()
    campos_only: tuple[str, ...] = ()
    orden: tuple[Any, ...] = ()

    def predicado(self, q: str) -> Q | None:
        """``Q`` combinado: prefijo en los CÓDIGO, subcadena en los NOMBRE.

        Devuelve ``None`` —y NO un ``Q()`` vacío— cuando ningún campo aplica. Es
        deliberado: ``filter(Q())`` no filtra nada y devolvería la tabla entera del
        alcance del usuario, que es exactamente lo que un buscador no debe hacer.
        """
        condicion = Q()
        aplicado = False
        for campo in self.campos_codigo:
            condicion |= Q(**{f"{campo}__istartswith": q})
            aplicado = True
        if len(q) >= LONGITUD_MINIMA_NOMBRE:
            for campo in self.campos_nombre:
                condicion |= Q(**{f"{campo}__icontains": q})
                aplicado = True
        return condicion if aplicado else None

    def buscar(self, user, q: str, limite: int) -> tuple[list, bool]:
        """Devuelve ``(filas, hay_mas)`` para esta entidad.

        Se piden ``limite + 1`` filas para saber si hay más sin pagar un ``COUNT``
        aparte: en un dropdown basta con señalar que la lista está recortada.
        """
        condicion = self.predicado(q)
        if condicion is None:
            return [], False
        qs = self.alcance(user).filter(condicion)
        if self.campos_only:
            qs = qs.only(*self.campos_only)
        if self.orden:
            qs = qs.order_by(*self.orden)
        objetos = list(qs[: limite + 1])
        hay_mas = len(objetos) > limite
        return [self.fila(obj) for obj in objetos[:limite]], hay_mas


def _texto(valor) -> str | None:
    """Normaliza a ``None`` los vacíos: varias de estas columnas son ``blank=True``."""
    if valor is None:
        return None
    valor = str(valor).strip()
    return valor or None


def _fila_pedido(pedido) -> dict:
    # ``Pedido.folio`` es NULL-able: un pedido en BORRADOR todavía no pasó por
    # ``_asignar_folio()``. Si además coincide por los campos NOMBRE, la fila
    # llegaría al dropdown con la línea principal vacía; por eso ``titulo`` cae al
    # nombre del cliente. ``codigo`` sí se queda en ``None``: no hay folio que dar.
    cliente = _texto(pedido.cliente_razon_social) or _texto(pedido.cliente_nombre)
    folio = _texto(pedido.folio)
    return {
        "tipo": "pedido",
        "id": pedido.pk,
        "codigo": folio,
        "titulo": folio or cliente,
        "subtitulo": cliente if folio else None,
        "estatus": pedido.get_estatus_display(),
    }


def _fila_cliente(cliente) -> dict:
    return {
        "tipo": "cliente",
        "id": cliente.pk,
        "codigo": None,
        "titulo": _texto(cliente.nombre) or _texto(cliente.razon_social),
        "subtitulo": _texto(cliente.razon_social) or _texto(cliente.correo),
        "estatus": None,
    }


def _fila_cotizacion(cotizacion) -> dict:
    # ``Cotizacion`` no tiene folio (se confirmó contra el modelo): ``codigo`` va en
    # ``None`` a propósito y la identidad viaja en ``id``, que es como la referencia
    # hoy el resto de la API (el filtro ``?q=`` numérico busca por ``id``).
    cliente = cotizacion.cliente
    return {
        "tipo": "cotizacion",
        "id": cotizacion.pk,
        "codigo": None,
        "titulo": (
            _texto(getattr(cliente, "razon_social", None))
            or _texto(getattr(cliente, "nombre", None))
        ),
        "subtitulo": _texto(cotizacion.oc),
        "estatus": cotizacion.get_estatus_display(),
    }


#: Orden del registro = orden de los grupos en la respuesta.
REGISTRO: tuple[EntidadBuscable, ...] = (
    EntidadBuscable(
        tipo="pedido",
        etiqueta="Pedidos",
        # ``pedidos_base()`` es el MISMO queryset que usa ``PedidoViewSet``: si
        # mañana gana un filtro, el buscador lo hereda en vez de divergir.
        alcance=lambda user: pedidos_visibles(pedidos_base(), user),
        fila=_fila_pedido,
        campos_codigo=("folio",),
        # ``cliente_nombre``/``cliente_razon_social`` son columnas SNAPSHOT del propio
        # ``pedidos`` (no FKs), así que no hay JOIN y los índices GIN son suyos.
        campos_nombre=("cliente_nombre", "cliente_razon_social"),
        campos_only=("folio", "cliente_nombre", "cliente_razon_social", "estatus"),
        orden=ORDEN_RECIENTE,
    ),
    EntidadBuscable(
        tipo="cliente",
        etiqueta="Clientes",
        alcance=lambda user: clientes_visibles(clientes_base(), user),
        fila=_fila_cliente,
        campos_nombre=("nombre", "razon_social", "correo"),
        campos_only=("nombre", "razon_social", "correo"),
        orden=("nombre", "id"),
    ),
    EntidadBuscable(
        tipo="cotizacion",
        etiqueta="Cotizaciones",
        # ``select_related("cliente")``: la fila muestra el nombre del cliente y es
        # además donde se busca. Sin esto habría un N+1 por resultado.
        alcance=lambda user: cotizaciones_visibles(
            cotizaciones_base().select_related("cliente"), user
        ),
        fila=_fila_cotizacion,
        # No tiene folio: sólo se busca a través del cliente relacionado. Estos dos
        # campos los sirven los mismos índices GIN de ``clientes``.
        campos_nombre=("cliente__nombre", "cliente__razon_social"),
        campos_only=("oc", "estatus", "cliente__nombre", "cliente__razon_social"),
        orden=ORDEN_RECIENTE,
    ),
)


class BusquedaGlobalAPIView(APIView):
    """Buscador federado del header. Sólo lectura."""

    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        operation_id="busqueda_global",
        description=(
            "Busca en varias entidades a la vez y devuelve los resultados agrupados "
            "por tipo. Con `q` por debajo de la longitud mínima devuelve los grupos "
            "vacíos, no un error. Los campos de nombre requieren `longitud_minima_"
            "nombre` caracteres; por debajo sólo se consultan los de código."
        ),
        parameters=[
            OpenApiParameter(
                "q",
                OpenApiTypes.STR,
                description=f"Texto a buscar. Mínimo {LONGITUD_MINIMA_Q} caracteres.",
            ),
            OpenApiParameter(
                "limit",
                OpenApiTypes.INT,
                description=(
                    f"Resultados por grupo. Por defecto {LIMITE_POR_DEFECTO}, "
                    f"tope {LIMITE_MAXIMO}."
                ),
            ),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request):
        q = (request.query_params.get("q") or "").strip()
        limite = entero_acotado(
            request.query_params.get("limit"),
            por_defecto=LIMITE_POR_DEFECTO,
            minimo=1,
            maximo=LIMITE_MAXIMO,
        )
        suficiente = len(q) >= LONGITUD_MINIMA_Q

        grupos = []
        for entidad in REGISTRO:
            if suficiente:
                resultados, hay_mas = entidad.buscar(request.user, q, limite)
            else:
                resultados, hay_mas = [], False
            grupos.append(
                {
                    "tipo": entidad.tipo,
                    "etiqueta": entidad.etiqueta,
                    "resultados": resultados,
                    "hay_mas": hay_mas,
                }
            )

        return Response(
            {
                "q": q,
                "limit": limite,
                "longitud_minima": LONGITUD_MINIMA_Q,
                # Umbral real de los campos de nombre: por debajo de él sólo se
                # buscan códigos, así que un grupo sin campos de código sale vacío
                # aunque ``q`` supere ``longitud_minima``.
                "longitud_minima_nombre": LONGITUD_MINIMA_NOMBRE,
                "grupos": grupos,
            }
        )
