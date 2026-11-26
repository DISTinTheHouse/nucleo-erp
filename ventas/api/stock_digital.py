"""Stock digital: ``/api/v1/ventas/stock-digital/`` (solo lectura).

Para Vendedores (CRM) y Mesa de Control: por SKU, la existencia real, lo
comprometido, lo que viene de producción y de compras, y la disponibilidad
digital que resulta. Las reglas están en ``ventas/services/stock_digital.py``.
Solo informa: no aparta piezas, no mueve inventario y no cambia cómo se autoriza
ni cómo se surte un pedido.

Alcance: la empresa activa del usuario, superusuario incluido (el stock es de una
empresa); sin empresa no se ve nada. Códigos: ``CODIGOS_STOCK_DIGITAL``.

Listado (50 por página, ``?page_size=`` hasta 200), SKUs activos de producto
terminado ordenados por SKU:

- ``?q=`` SKU, nombre de la variante o del producto.
- ``?producto=``, ``?categoria=``, ``?color=``, ``?talla=`` (ids).
- ``?variantes=1,2,3``: esos SKUs (hasta 200), aunque estén inactivos.
- ``?sucursal=``: solo existencias, pedidos, OP y OC de esa sucursal.
- ``?solo=disponibles|faltantes|comprometidos|entradas|existencia``.

Detalle ``/{variante_id}/``: los mismos totales y de dónde salen (existencia por
almacén, pedidos que la comprometen con su entrega estimada, OP y OC por llegar).

Costo: el listado hace las mismas consultas sin importar cuántos SKUs trae.
"""
from django.db.models import Q
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from catalogo.models import ProductoVariante
from nucleo.kpis import entero_estricto, entero_param
from nucleo.models import Sucursal
from nucleo.permisos import CODIGOS_STOCK_DIGITAL, LecturaPorCodigo
from ventas.services import stock_digital as servicio

#: Lo que se vende y lo único que piden los pedidos.
TIPO_PRODUCTO_TERMINADO = "PT"
MAX_VARIANTES = 200
#: Filtros por id del listado y su campo.
FILTROS_ID = (
    ("producto", "producto_id"),
    ("categoria", "producto__categoria_producto_id"),
    ("color", "color_id"),
    ("talla", "talla_id"),
)


class _Pagina(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


def _fila(v, totales):
    producto = v.producto
    return {
        "variante_id": v.pk,
        "sku": v.sku,
        "nombre": v.nombre,
        "producto_id": v.producto_id,
        "producto_nombre": producto.nombre,
        "categoria_id": producto.categoria_producto_id,
        "categoria_nombre": getattr(producto.categoria_producto, "nombre", None),
        "color_id": v.color_id,
        "color": v.color.nombre,
        "talla_id": v.talla_id,
        "talla": getattr(v.talla, "nombre", None),
        "activo": v.activo and producto.activo,
        **totales,
    }


class StockDigitalViewSet(GenericViewSet):
    permission_classes = [IsAuthenticated, LecturaPorCodigo]
    codigos_lectura = {"list": CODIGOS_STOCK_DIGITAL, "retrieve": CODIGOS_STOCK_DIGITAL}
    pagination_class = _Pagina
    filter_backends = []
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        empresa_id = getattr(self.request.user, "empresa_id", None)
        if not empresa_id:
            return ProductoVariante.objects.none()
        return ProductoVariante.objects.filter(empresa_id=empresa_id)

    def _sucursal(self):
        sucursal_id = entero_param(self.request, "sucursal", positivo=True)
        if sucursal_id and not Sucursal.objects.filter(
            pk=sucursal_id, empresa_id=self.request.user.empresa_id,
        ).exists():
            raise ValidationError({"sucursal": "No es una sucursal de tu empresa."})
        return sucursal_id

    def _variantes(self):
        texto = (self.request.query_params.get("variantes") or "").strip()
        if not texto:
            return None
        partes = [x for x in texto.split(",") if x.strip()]
        if len(partes) > MAX_VARIANTES:
            raise ValidationError({"variantes": f"Máximo {MAX_VARIANTES} ids."})
        return {entero_estricto(x, "variantes", positivo=True) for x in partes}

    def _filtrar(self, qs):
        variantes = self._variantes()
        if variantes is None:
            qs = qs.filter(activo=True, producto__activo=True, producto__tipo__codigo=TIPO_PRODUCTO_TERMINADO)
        else:
            qs = qs.filter(pk__in=variantes)
        for nombre, campo in FILTROS_ID:
            valor = entero_param(self.request, nombre, positivo=True)
            if valor:
                qs = qs.filter(**{campo: valor})
        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(sku__icontains=q) | Q(nombre__icontains=q) | Q(producto__nombre__icontains=q))
        return qs

    def list(self, request, *args, **kwargs):
        solo = (request.query_params.get("solo") or "").strip().lower()
        if solo and solo not in servicio.FILTROS_SOLO:
            raise ValidationError({"solo": f"Usa uno de: {', '.join(servicio.FILTROS_SOLO)}."})
        sucursal_id = self._sucursal()
        empresa_id = request.user.empresa_id
        qs = self._filtrar(self.get_queryset())
        ids = list(qs.order_by("sku", "pk").values_list("pk", flat=True))
        datos = {}
        if solo and ids:
            # El filtro depende de los totales: se calculan para todo el filtro (mismas 4 consultas).
            datos = servicio.totales(empresa_id, qs.values("pk"), sucursal_id)
            cumple = servicio.FILTROS_SOLO[solo]
            ids = [i for i in ids if cumple(datos.get(i, servicio.TOTALES_VACIOS))]
        pagina = self.paginate_queryset(ids)
        if pagina and not solo:
            datos = servicio.totales(empresa_id, pagina, sucursal_id)
        variantes = ProductoVariante.objects.select_related(
            "producto__categoria_producto", "color", "talla",
        ).in_bulk(pagina) if pagina else {}
        return self.get_paginated_response([
            _fila(variantes[i], datos.get(i, servicio.TOTALES_VACIOS)) for i in pagina
        ])

    def retrieve(self, request, *args, **kwargs):
        try:
            pk = entero_estricto(self.kwargs.get(self.lookup_field), "id", positivo=True)
        except ValidationError:
            pk = None
        variante = (
            self.get_queryset().select_related("producto__categoria_producto", "color", "talla")
            .filter(pk=pk).first() if pk else None
        )
        if variante is None:
            raise NotFound("No existe ese SKU en tu empresa.")
        d = servicio.detalle(variante.empresa_id, variante.pk, self._sucursal())
        return Response({**_fila(variante, d.pop("totales")), **d})
