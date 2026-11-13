from django.db.models import F
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Lower
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response
from catalogo.codigos import siguiente_codigo_producto
from catalogo.tallas import talla_sort_key
from catalogo.validaciones import talla_permitida_para_producto
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante, CategoriaProductoProceso, VarianteProductoProduccion
from catalogo.api.serializers import REGLA_DUPLICADA, CategoriaProductoProcesoSerializer, TipoProductoSerializer, CategoriaProductoSerializer, ColorSerializer, TallaSerializer, ProductoSerializer, ProductoOnboardingSerializer, ProductoSelectorSerializer,ProductoVarianteSerializer, ProductoVarianteOnboardingSerializer, ProductoVarianteSelectorSerializer
from nucleo.kpis import MENSAJE_ENTERO, entero_estricto, vacio
from nucleo.listados import OrdenamientoEstableFilter, PaginacionOpcionalLimitOffset, booleano_param
from produccion.models import ListaMaterialBom

# Solo Producto Terminado exige talla en el onboarding de variante; materia
# prima, compras, etc. no tienen tallas. Se resuelve por ``TipoProducto.codigo``
# (único), no por id: el id depende del entorno (#910).
CODIGO_PRODUCTO_TERMINADO = "PT"

# Productos y variantes (EC-886): ``?ordering=`` con lista blanca y ``id`` como
# desempate (``OrdenamientoEstableFilter``). Sin lista blanca, ``OrderingFilter``
# aceptaba cualquier campo del serializer y sin desempate.
FILTROS_CON_ORDEN_ESTABLE = [
    *[b for b in viewsets.ModelViewSet.filter_backends if b is not OrderingFilter],
    OrdenamientoEstableFilter,
]

# ``?linea=SIN_LINEA`` pide la línea vacía: materia prima (TELAS, AVIOS) (#923).
SIN_LINEA = "SIN_LINEA"


def _alcance_empresa(qs, user):
    # Aislamiento multi-tenant: todos, superusuario incluido, ven sólo su empresa
    # activa (#1060); quien no tiene empresa no ve nada. Detalle ajeno -> 404,
    # listado -> 200 [].
    empresa = getattr(user, "empresa", None)
    if empresa:
        return qs.filter(empresa=empresa)
    return qs.none()


class PaginacionOpcionalCatalogo(PaginacionOpcionalLimitOffset):
    """``PaginacionOpcionalLimitOffset`` con el schema real de ``producto/`` y
    ``producto-variante/`` (EC-886): sin ``?limit=`` regresan el arreglo plano de
    siempre; con él, ``{count, next, previous, results}``. drf-spectacular
    documentaba sólo el sobre."""

    def get_paginated_response_schema(self, schema):
        return {
            "oneOf": [schema, super().get_paginated_response_schema(schema)],
            "description": "Arreglo plano sin ``limit``; sobre paginado con ``limit``.",
        }


class PaginacionSelector(PaginacionOpcionalLimitOffset):
    """``selector/`` (EC-886) siempre pagina: sin ``?limit=`` usa ``default_limit``.
    Mismas reglas que la paginación opcional (400 con valores inválidos, tope 500).
    50 es el tamaño por defecto de la otra paginación opcional del ERP
    (``produccion.api.views.PaginacionOpcional``); el selector puede pedir más."""

    default_limit = 50

    def get_limit(self, request):
        # Con ``?ids=`` y sin ``?limit=``, una sola página con todos los pedidos
        # (``TOPE_IDS_SELECTOR`` como máximo): el selector resuelve la etiqueta de
        # todo lo ya elegido aunque pase de 50. Un ``limit`` explícito manda.
        if vacio(request.query_params.get(self.limit_query_param)):
            ids = _ids_param(request)
            if ids is not None:
                return len(ids)
        return super().get_limit(request)


# Tope de ``?ids=`` en ``selector/``: el mismo que ``limit``, una página llena.
TOPE_IDS_SELECTOR = PaginacionSelector.max_limit


def _ids_param(request):
    """``?ids=1,2,3`` de ``selector/`` (EC-886); ausente o vacío → ``None``.

    Se parsea una vez por petición y se guarda en el ``request``: la acción, el
    queryset y ``PaginacionSelector.get_limit`` leen el mismo resultado."""
    if not hasattr(request, "_ids_selector"):
        request._ids_selector = _parsear_ids(request)
    return request._ids_selector


def _parsear_ids(request):
    """Cada id con el parser estricto de EC-829 (``entero_estricto``, positivo): un
    token inválido o vacío (``1,,2``) es 400, y también más de ``TOPE_IDS_SELECTOR``."""
    crudo = request.query_params.get("ids")
    if vacio(crudo):
        return None
    return _ids_de_tokens(crudo.split(","), "ids")


def _ids_de_tokens(tokens, nombre):
    """Ids enteros positivos (``entero_estricto``) de ``tokens``, con 400 bajo la clave
    ``nombre`` si alguno es inválido o vacío o si pasan de ``TOPE_IDS_SELECTOR``."""
    if len(tokens) > TOPE_IDS_SELECTOR:
        raise ValidationError({nombre: f"Máximo {TOPE_IDS_SELECTOR} ids."})
    ids = [entero_estricto(token, nombre, positivo=True) for token in tokens]
    if None in ids:
        raise ValidationError({nombre: MENSAJE_ENTERO})
    return ids


def _ids_filtro(request, nombre):
    """Filtro por FK (EC-896): ``?{nombre}=3``, ``?{nombre}=1,3`` o repetido
    (``?{nombre}=1&{nombre}=3``). Ausente o vacío → ``None`` (sin filtro, como
    ``entero_param``); un token inválido o vacío (``1,``) → 400; tope
    ``TOPE_IDS_SELECTOR``. No comprueba que los ids existan: el queryset ya está
    acotado a la empresa, así que un id ajeno responde igual que uno inexistente."""
    crudos = [crudo for crudo in request.query_params.getlist(nombre) if not vacio(crudo)]
    if not crudos:
        return None
    return _ids_de_tokens([token for crudo in crudos for token in crudo.split(",")], nombre)


def _filtrar_por_ids(qs, request, nombre, campo=None):
    ids = _ids_filtro(request, nombre)
    if ids is None:
        return qs
    return qs.filter(**{f"{campo or nombre}_id__in": ids})


def _precio_param(request, nombre, modelo):
    """``precio_base_min``/``precio_base_max`` con la precisión de ``modelo.precio_base``
    (el ``DecimalField`` de DRF que valida la escritura, como ``_importe`` del listado
    de pedidos); vacío → ``None``; inválido o fuera de rango → 400."""
    valor = (request.query_params.get(nombre) or "").strip()
    if not valor:
        return None
    campo = modelo._meta.get_field("precio_base")
    validador = serializers.DecimalField(max_digits=campo.max_digits, decimal_places=campo.decimal_places)
    try:
        return validador.run_validation(valor)
    except ValidationError as error:
        raise ValidationError({nombre: error.detail})


def _filtrar_precio(qs, request):
    """``?precio_base_min=`` (inclusivo) y ``?precio_base_max=`` (exclusivo), como los
    rangos del listado de pedidos y ``matchesRangeFilter`` del front. Un precio NULL
    no cae en ningún rango."""
    minimo = _precio_param(request, "precio_base_min", qs.model)
    maximo = _precio_param(request, "precio_base_max", qs.model)
    if minimo is not None:
        qs = qs.filter(precio_base__gte=minimo)
    if maximo is not None:
        qs = qs.filter(precio_base__lt=maximo)
    return qs


def _respuesta_selector(view, qs, serializer_class):
    # ``view.paginator`` es ``PaginacionSelector`` (``pagination_class`` de la acción).
    pagina = view.paginate_queryset(qs)
    return view.get_paginated_response(serializer_class(pagina, many=True).data)


def _param_texto(nombre, descripcion):
    return OpenApiParameter(nombre, OpenApiTypes.STR, OpenApiParameter.QUERY, description=descripcion)


PARAM_IDS_SELECTOR = _param_texto(
    "ids",
    "Ids separados por comas (máx. 500), para resolver la etiqueta de lo ya elegido. "
    "Regresa cada id de la empresa del usuario, activo o no, sin aplicar los demás "
    "filtros; sin ``limit`` todos caben en una página.",
)
PARAM_Q = _param_texto("q", "Búsqueda por subcadena, sin distinguir mayúsculas.")


def _param_ids(nombre, que):
    return _param_texto(nombre, f"Uno o varios ids de {que}, separados por comas (máx. 500).")


PARAMS_PRECIO = [
    _param_texto("precio_base_min", "Precio base mínimo, inclusivo."),
    _param_texto("precio_base_max", "Precio base máximo, exclusivo."),
]
# Filtros de producto compartidos por el listado y ``selector/``.
PARAMS_PRODUCTO = [
    _param_texto("tipo_id", "Uno o varios ids de tipo de producto, separados por comas."),
    _param_texto("linea", "Una o varias líneas de categoría, separadas por comas; SIN_LINEA = sin línea."),
    _param_texto("es_compras", "true o false."),
    _param_ids("categoria_producto", "categoría de producto"),
]
# Filtros de variante compartidos por el listado y ``selector/``.
PARAMS_VARIANTE = [
    _param_texto("con_bom", "true: sólo variantes con BOM activo."),
    _param_ids("producto", "producto"),
    _param_ids("color", "color"),
    _param_ids("talla", "talla"),
]


def _lineas_param(request):
    """``?linea=`` (#922): uno o varios, repetido o separado por comas, sin
    distinguir mayúsculas; vacío no filtra. ``SIN_LINEA`` se traduce a la línea
    vacía (#923). Un valor fuera de ``CategoriaProducto.Linea`` es 400."""
    valores = {
        valor.strip().upper()
        for crudo in request.query_params.getlist("linea")
        for valor in crudo.split(",")
    } - {""}
    validos = [*CategoriaProducto.Linea.values, SIN_LINEA]
    if valores - set(validos):
        raise ValidationError({"linea": f"Valor inválido. Usa uno de: {', '.join(validos)}."})
    return ["" if valor == SIN_LINEA else valor for valor in valores]


class SoloSuperusuarioEscribe(BasePermission):
    """Catálogo global (sin empresa): todos leen, solo el superusuario escribe (#535)."""

    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or request.user.is_superuser


class SoloAdminEmpresaEscribe(BasePermission):
    """Todos leen; solo el superusuario o el admin de empresa escriben (EC-575)."""

    def has_permission(self, request, view):
        user = request.user
        return request.method in SAFE_METHODS or bool(
            getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)
        )


class TipoProductoViewSet(viewsets.ModelViewSet):
    """Tipos (MP, PT, ...). Global; ``DELETE`` = baja lógica (``activo=False``).

    El listado trae los activos y ``?activo=false`` las bajas; el detalle ve
    ambos, así que una baja se consulta y se reactiva con
    ``PATCH {"activo": true}`` (#907)."""
    serializer_class = TipoProductoSerializer
    permission_classes = [IsAuthenticated, SoloSuperusuarioEscribe]

    def get_queryset(self):
        qs = TipoProducto.objects.order_by("codigo")
        if self.action == "list":
            qs = qs.filter(activo=booleano_param(self.request, "activo", True))
        return qs

    def perform_destroy(self, instance):
        instance.activo = False
        instance.save(update_fields=["activo"])

class CategoriaProductoViewSet(viewsets.ModelViewSet):
    """Por empresa: todos leen, escriben el admin de empresa y el superusuario (#921)."""
    serializer_class = CategoriaProductoSerializer
    permission_classes = [IsAuthenticated, SoloAdminEmpresaEscribe]

    def get_queryset(self):
        # ``tallas`` (M2M) en bloque: sin el prefetch era una consulta por fila.
        qs = CategoriaProducto.objects.filter(activo=True).prefetch_related("tallas").order_by("-created_at", "-id")
        lineas = _lineas_param(self.request)
        if lineas:
            qs = qs.filter(linea__in=lineas)
        return _alcance_empresa(qs, self.request.user)

    def perform_destroy(self, instance):
        # Baja lógica (#920): el DELETE físico arrastraba en cascada los
        # productos de la categoría y, con ellos, compras, producción y ventas.
        instance.activo = False
        instance.save(update_fields=["activo", "updated_at"])

# Nombre del ``UniqueConstraint`` parcial de ``CategoriaProductoProceso`` (models.py).
UNIQUE_REGLA_ACTIVA = "uq_categoria_proceso_ubicacion_activo"


class CategoriaProductoProcesoViewSet(viewsets.ModelViewSet):
    """Matriz prenda/proceso (EC-572). Filtro opcional ``?categoria_producto=``; baja lógica."""

    serializer_class = CategoriaProductoProcesoSerializer
    permission_classes = [IsAuthenticated, SoloAdminEmpresaEscribe]

    def get_queryset(self):
        qs = CategoriaProductoProceso.objects.filter(activo=True).order_by("categoria_producto_id", "proceso", "ubicacion")
        categoria_id = self.request.query_params.get("categoria_producto")
        if categoria_id:
            try:
                qs = qs.filter(categoria_producto_id=int(categoria_id))
            except (TypeError, ValueError):
                raise ValidationError({"categoria_producto": "Debe ser un entero."})
        return _alcance_empresa(qs, self.request.user)

    def perform_create(self, serializer):
        self._guardar_sin_duplicar(serializer)

    def perform_update(self, serializer):
        self._guardar_sin_duplicar(serializer)

    @staticmethod
    def _guardar_sin_duplicar(serializer):
        # El serializer ya rechaza el duplicado; esto cubre la carrera entre dos
        # altas simultáneas, que llega al ``UniqueConstraint`` (EC-575).
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError as exc:
            # Solo la violación del unique parcial es un duplicado; cualquier otra
            # (FK, NOT NULL...) sigue su curso.
            if UNIQUE_REGLA_ACTIVA not in str(exc):
                raise
            raise ValidationError({"non_field_errors": [REGLA_DUPLICADA]})

    def perform_destroy(self, instance):
        instance.activo = False
        instance.save(update_fields=["activo", "updated_at"])


class ColorViewSet(viewsets.ModelViewSet):
    serializer_class = ColorSerializer
    permission_classes = [IsAuthenticated, SoloSuperusuarioEscribe]

    def get_queryset(self):
        return Color.objects.filter(activo=True).order_by(Lower("nombre"), "id")

    def perform_destroy(self, instance):
        # Baja lógica (#914): el borrado físico arrastraba en cascada variantes
        # de todas las empresas, igual que la talla.
        instance.activo = False
        instance.save(update_fields=["activo"])

class TallaViewSet(viewsets.ModelViewSet):
    serializer_class = TallaSerializer
    permission_classes = [IsAuthenticated, SoloSuperusuarioEscribe]

    def perform_destroy(self, instance):
        # Baja lógica: el borrado físico arrastraba en cascada variantes de todas las empresas.
        instance.activo = False
        instance.save(update_fields=["activo"])

    def get_queryset(self):
        qs = Talla.objects.filter(activo=True)
        categoria_id = self.request.query_params.get('categoria_producto')
        if categoria_id:
            try:
                categoria_id = int(categoria_id)
            except (TypeError, ValueError):
                raise ValidationError({"categoria_producto": "Debe ser un entero."})
            # La categoría se acota a la empresa del usuario y activa: una ajena o
            # inexistente responde igual, lista vacía (#760).
            categorias = _alcance_empresa(CategoriaProducto.objects.filter(activo=True), self.request.user)
            qs = qs.filter(categorias_producto__in=categorias.filter(pk=categoria_id)).distinct()
        return qs

    def list(self, request, *args, **kwargs):
        # El orden canónico sale de ``nombre`` y no se expresa en SQL: se ordena
        # en Python solo en ``list`` (``get_queryset`` debe seguir siendo un
        # queryset para retrieve/update). Un ``?ordering=`` válido manda; uno que
        # ``OrderingFilter`` descarta deja el queryset sin orden y aplica el canónico.
        queryset = self.filter_queryset(self.get_queryset())
        if not queryset.ordered:
            queryset = sorted(queryset, key=lambda talla: talla_sort_key(talla.nombre))
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

@extend_schema_view(list=extend_schema(parameters=[
    PARAM_Q,
    *PARAMS_PRODUCTO,
    *PARAMS_PRECIO,
    _param_texto("con_cod_proscai", "true: con código Proscai; false: sin él (vacío)."),
]))
class ProductoViewSet(viewsets.ModelViewSet):
    queryset = Producto.objects.all()
    serializer_class = ProductoSerializer
    # Sin ``?limit=`` regresa la lista completa, como antes (EC-886).
    pagination_class = PaginacionOpcionalCatalogo
    filter_backends = FILTROS_CON_ORDEN_ESTABLE
    # Sin ``?ordering=`` válido manda el orden de ``get_queryset`` (``-created_at, -id``).
    # ``categoria_producto`` ordena por id (se conserva por compatibilidad); las
    # columnas Categoría y Línea de la tabla ordenan por nombre y línea (EC-896).
    ordering_fields = [
        "id", "nombre", "codigo", "cod_proscai", "descripcion", "precio_base", "activo",
        "es_compras", "tipo", "categoria_producto", "categoria_producto__nombre",
        "categoria_producto__linea", "created_at", "updated_at",
    ]

    def get_queryset(self):
        qs = self._productos_de_la_empresa().filter(activo=True)
        # Los filtros del query string sólo en el listado (EC-896): en detalle,
        # edición y baja un ``?q=`` o ``?tipo_id=`` respondía 404 sobre un producto
        # visible. ``selector/`` arma su propio queryset.
        if self.action == "list":
            qs = self._filtrar_listado(self._filtrar(qs, buscar_en_descripcion=False))
        return qs

    def _productos_de_la_empresa(self):
        """Productos de la empresa del usuario (activos o no), con ``unidad_*`` y el
        orden del listado. Sin filtros de query string: los pone ``_filtrar``."""
        qs = super().get_queryset().annotate(
            unidad_nombre=F('unidad_medida__nombre'), unidad_abreviatura=F('unidad_medida__clave'),
        )
        return _alcance_empresa(qs, self.request.user).order_by("-created_at", "-id")

    def _filtrar(self, qs, *, buscar_en_descripcion):
        """Filtros compartidos por el listado y ``selector/``: ``tipo_id``,
        ``categoria_producto``, ``linea``, ``es_compras`` y ``q``.
        ``buscar_en_descripcion``: ``q`` busca también en ``descripcion`` (sólo el
        selector, EC-886; el ``q`` del listado no cambia)."""
        # ``tipo_id`` y ``categoria_producto`` aceptan uno o varios: repetido
        # (``?tipo_id=1&tipo_id=3``) o separado por comas (``?tipo_id=1,3``), con el
        # parser estricto de EC-829 (EC-896).
        qs = _filtrar_por_ids(qs, self.request, "tipo_id", "tipo")
        qs = _filtrar_por_ids(qs, self.request, "categoria_producto")
        lineas = _lineas_param(self.request)
        if lineas:
            filtro = Q(categoria_producto__linea__in=lineas)
            if "" in lineas:
                # Sin categoría tampoco hay línea: entra en ``SIN_LINEA``.
                filtro |= Q(categoria_producto__isnull=True)
            qs = qs.filter(filtro)
        es_compras = (self.request.query_params.get('es_compras') or '').strip().lower()
        if es_compras:
            if es_compras not in ('true', 'false', '1', '0'):
                raise ValidationError({"es_compras": "Usa true o false."})
            qs = qs.filter(es_compras=es_compras in ('true', '1'))
        q = (self.request.query_params.get('q') or '').strip()
        if q:
            filtro_q = Q(nombre__icontains=q) | Q(codigo__icontains=q) | Q(cod_proscai__icontains=q)
            if buscar_en_descripcion:
                # El paso 1 del BOM también busca por descripción.
                filtro_q |= Q(descripcion__icontains=q)
            qs = qs.filter(filtro_q)
        return qs

    def _filtrar_listado(self, qs):
        """Filtros sólo del listado (EC-896): ``precio_base_min``/``precio_base_max`` y
        ``con_cod_proscai``. "Sin código" es vacío o sólo espacios (la columna es
        NOT NULL), lo mismo que el filtro de presencia del front."""
        qs = _filtrar_precio(qs, self.request)
        con_cod_proscai = booleano_param(self.request, "con_cod_proscai", None)
        if con_cod_proscai is not None:
            sin_codigo = Q(cod_proscai__regex=r"^\s*$")
            qs = qs.exclude(sin_codigo) if con_cod_proscai else qs.filter(sin_codigo)
        return qs

    @extend_schema(
        summary="Productos para selectores (ligero y paginado)",
        responses=ProductoSelectorSerializer(many=True),
        parameters=[PARAM_Q, PARAM_IDS_SELECTOR, *PARAMS_PRODUCTO],
    )
    @action(detail=False, methods=['get'], pagination_class=PaginacionSelector)
    def selector(self, request):
        """Productos para selectores (EC-886), siempre paginado (``?limit``/``?offset``,
        50 por defecto, tope 500). Mismo alcance, filtros (``tipo_id``,
        ``categoria_producto``, ``linea``, ``es_compras``; no los de precio ni
        ``con_cod_proscai``) y ``?ordering=`` que el listado; ``q`` busca además en
        ``descripcion``. Publica ``id, nombre, codigo, descripcion, activo, unidad_id,
        unidad_nombre, unidad_abreviatura, tipo, categoria_producto``.

        Sólo productos activos, salvo con ``?ids=1,2``: entonces regresa esos, activos o
        no, sin aplicar ``q`` ni los demás filtros, siempre de la empresa del usuario."""
        ids = _ids_param(request)
        qs = self._productos_de_la_empresa()
        if ids is not None:
            # Sólo el alcance por empresa: lo ya elegido conserva su etiqueta aunque
            # esté inactivo o no coincida con la búsqueda o los filtros actuales.
            qs = qs.filter(pk__in=ids)
        else:
            qs = self._filtrar(qs.filter(activo=True), buscar_en_descripcion=True)
        return _respuesta_selector(self, self.filter_queryset(qs), ProductoSelectorSerializer)

    def perform_destroy(self, instance):
        # Baja lógica (#531): el DELETE físico arrastraba en cascada compras,
        # inventario, producción y ventas del producto.
        instance.activo = False
        instance.save(update_fields=["activo", "updated_at"])

    def perform_create(self, serializer):
        # Mismo codigo que /onboarding/: sale del servidor via categoria, nunca
        # del cliente (issue #231 -- el alta legacy no lo generaba y quedaban
        # productos con codigo=null que /producto-variante/onboarding/ rechaza).
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        extra = {}
        if not empresa:
            raise ValidationError({"empresa": "No tienes una empresa activa."})
        extra["empresa"] = empresa

        categoria = serializer.validated_data.get("categoria_producto")
        if categoria is not None:
            with transaction.atomic():
                extra["codigo"] = siguiente_codigo_producto(categoria)
                serializer.save(**extra)
            return
        serializer.save(**extra)

    def _categoria_del_request(self, request, categoria_id):
        if not categoria_id:
            raise ValidationError({"categoria_producto": "Este parametro es requerido."})
        try:
            categoria = CategoriaProducto.objects.get(pk=categoria_id)
        except (CategoriaProducto.DoesNotExist, ValueError, TypeError):
            raise ValidationError({"categoria_producto": "No existe."})

        user = request.user
        empresa = getattr(user, "empresa", None)
        # Sin empresa (y sin ser superusuario) se rechaza, no se salta la guarda.
        if (empresa is None or categoria.empresa_id != empresa.pk):
            raise ValidationError({"categoria_producto": "No pertenece a tu empresa."})
        return categoria

    @action(detail=False, methods=['get'], url_path='siguiente-codigo')
    def siguiente_codigo(self, request):
        # Preview de solo lectura para el formulario de alta: no reserva nada,
        # el codigo real se recalcula con lock al momento de crear en /onboarding/.
        categoria = self._categoria_del_request(request, request.query_params.get('categoria_producto'))
        codigo = siguiente_codigo_producto(categoria, lock=False)
        return Response({"codigo": codigo})

    @action(detail=False, methods=['post'])
    def onboarding(self, request):
        # Alta simplificada para producción: nombre + tipo + categoria + precio, sin descripcion.
        # codigo y unidad_medida se infieren de la categoria, no se piden.
        user = request.user
        empresa = getattr(user, "empresa", None)

        serializer = ProductoOnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        categoria = serializer.validated_data['categoria_producto']
        # Sin empresa (y sin ser superusuario) se rechaza: antes la guarda
        # ``empresa and ...`` se saltaba y el producto caía en la empresa ajena.
        if (empresa is None or categoria.empresa_id != empresa.pk):
            raise ValidationError({"categoria_producto": "No pertenece a tu empresa."})

        with transaction.atomic():
            codigo = siguiente_codigo_producto(categoria)
            # empresa siempre sale de la categoria (no de request.user): un
            # superusuario no tiene user.empresa, y aun asi el producto debe
            # quedar en la empresa dueña de la categoria que eligio.
            producto = serializer.save(
                codigo=codigo, unidad_medida=categoria.unidad_medida, empresa=categoria.empresa,
            )
        return Response(ProductoSerializer(producto).data, status=201)

@extend_schema_view(list=extend_schema(parameters=[
    PARAM_Q,
    *PARAMS_VARIANTE,
    OpenApiParameter("activo", OpenApiTypes.BOOL, OpenApiParameter.QUERY,
                     description="Sólo activas (true) o inactivas (false); sin él, ambas."),
    *PARAMS_PRECIO,
]))
class ProductoVarianteViewSet(viewsets.ModelViewSet):
    # producto_nombre/color_nombre/talla_nombre (agregados en 0599352) recorren las FK
    # producto, color y talla por su atributo .nombre. Con un queryset .all() sin
    # select_related, DRF resuelve esas relaciones de forma perezosa fila por fila: el
    # listado completo dispara 3 consultas por variante (N+1) y sobre la tabla real de
    # variantes el endpoint tarda tanto que parece colgarse. select_related las trae en el
    # mismo JOIN -> una sola consulta, sin alterar la forma de la respuesta.
    queryset = ProductoVariante.objects.all().select_related("producto", "color", "talla")
    serializer_class = ProductoVarianteSerializer
    # Sin ``?limit=`` regresa la lista completa, como antes (EC-886).
    pagination_class = PaginacionOpcionalCatalogo
    filter_backends = FILTROS_CON_ORDEN_ESTABLE
    # Sin ``?ordering=`` válido manda ``-id`` (``get_queryset``). ``producto__cod_proscai``
    # es el ``cod_proscai`` que publica el serializer; ``cod_proscai`` es el de la variante.
    ordering_fields = [
        "id", "sku", "nombre", "cod_proscai", "precio_base", "activo", "sku_produccion",
        "producto", "producto__nombre", "producto__cod_proscai", "color__nombre", "talla__nombre",
    ]

    def get_queryset(self):
        qs = self._variantes_de_la_empresa()
        # Los filtros del query string sólo en el listado (EC-896): en detalle,
        # edición y baja un ``?q=`` o ``?con_bom=`` respondía 404 sobre una variante
        # visible. ``selector/`` arma su propio queryset.
        if self.action == "list":
            qs = self._filtrar_listado(self._filtrar(qs))
        return qs

    def _variantes_de_la_empresa(self):
        """Variantes de la empresa del usuario con ``sku_produccion`` y orden ``-id``.
        Sin filtros de query string: los pone ``_filtrar``."""
        # ``ProductoVariante.empresa`` es FK propia (el onboarding la copia de
        # ``producto.empresa``); se acota por ella, no por ``producto__empresa``.
        qs = _alcance_empresa(super().get_queryset(), self.request.user)
        # Una muestra dada de alta en catálogo se sigue buscando por su SKU de
        # producción (``MP{linea}-{color}-{talla}``): se anota y entra en ``q``.
        qs = qs.annotate(sku_produccion=Subquery(
            VarianteProductoProduccion.objects.filter(
                activo=True, aplica_catalogo=True, producto_base=OuterRef('producto'),
                color=OuterRef('color'), talla=OuterRef('talla'),
            ).values('sku')[:1]
        ))
        # Antes sin ``order_by``: el orden dependía del plan de PostgreSQL (EC-886).
        return qs.order_by("-id")

    def _filtrar(self, qs):
        """Filtros compartidos por el listado y ``selector/``: ``con_bom``,
        ``producto``, ``color``, ``talla`` (uno o varios ids, EC-896) y ``q``."""
        if self.request.query_params.get('con_bom', '').lower() == 'true':
            qs = qs.filter(Exists(self._bom_activo()))
        for nombre in ("producto", "color", "talla"):
            qs = _filtrar_por_ids(qs, self.request, nombre)
        q = (self.request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(sku__icontains=q)
                | Q(cod_proscai__icontains=q)
                | Q(producto__cod_proscai__icontains=q)
                | Q(nombre__icontains=q)
                | Q(producto__nombre__icontains=q)
                | Q(sku_produccion__icontains=q)
            )
        return qs

    def _filtrar_listado(self, qs):
        """Filtros sólo del listado (EC-896): ``activo`` (sin él, activas e inactivas,
        como antes) y ``precio_base_min``/``precio_base_max``. ``selector/`` no los
        aplica: sólo ofrece activas."""
        activo = booleano_param(self.request, "activo", None)
        if activo is not None:
            qs = qs.filter(activo=activo)
        return _filtrar_precio(qs, self.request)

    def _bom_activo(self):
        """BOM activo de la variante (``OuterRef('pk')``) en la empresa del usuario:
        la misma subconsulta para ``?con_bom=`` y para ``tiene_bom`` del selector.

        Sin empresa no coincide nada (``empresa IS NULL`` en una FK NOT NULL): nunca
        cuenta BOM de otras empresas, aunque el queryset de fuera no esté acotado."""
        return ListaMaterialBom.objects.filter(
            producto_variante=OuterRef('pk'),
            activo=True,
            empresa=getattr(self.request.user, 'empresa', None),
        )

    @extend_schema(
        summary="Variantes para selectores (ligero y paginado)",
        responses=ProductoVarianteSelectorSerializer(many=True),
        parameters=[
            PARAM_Q,
            PARAM_IDS_SELECTOR,
            OpenApiParameter("tiene_bom", OpenApiTypes.BOOL, OpenApiParameter.QUERY,
                             description="Sólo variantes con (true) o sin (false) BOM activo."),
            *PARAMS_VARIANTE,
        ],
    )
    @action(detail=False, methods=['get'], pagination_class=PaginacionSelector)
    def selector(self, request):
        """Variantes para selectores (EC-886), siempre paginado (``?limit``/``?offset``,
        50 por defecto, tope 500). Mismo alcance, filtros (``con_bom``, ``producto``,
        ``color``, ``talla``, ``q``; no ``activo`` ni los de precio) y
        ``?ordering=`` que el listado, con ``id, sku, nombre, cod_proscai,
        sku_produccion, tiene_bom, activo``.

        Sólo variantes activas de productos activos, salvo con ``?ids=1,2``: entonces
        regresa esas, activas o no y sin aplicar ``q`` ni los demás filtros,
        siempre de la empresa del usuario, para que lo ya elegido conserve su etiqueta
        (con su ``tiene_bom``). ``?tiene_bom=true|false`` filtra por BOM activo."""
        ids = _ids_param(request)
        qs = self._variantes_de_la_empresa()
        if ids is not None:
            qs = qs.filter(pk__in=ids)
        else:
            qs = self._filtrar(qs).filter(activo=True, producto__activo=True)
        # Sólo las columnas que se publican: del producto basta su ``cod_proscai``,
        # anotado; sin JOIN que traiga la fila completa de producto, color y talla.
        qs = qs.select_related(None).only('id', 'sku', 'nombre', 'activo').annotate(
            tiene_bom=Exists(self._bom_activo()),
            producto_cod_proscai=F('producto__cod_proscai'),
        )
        if ids is None:
            tiene_bom = booleano_param(request, 'tiene_bom', None)
            if tiene_bom is not None:
                qs = qs.filter(tiene_bom=tiene_bom)
        return _respuesta_selector(self, self.filter_queryset(qs), ProductoVarianteSelectorSerializer)

    @action(detail=False, methods=['post'])
    def onboarding(self, request):
        # Alta simplificada de variante: producto + color + talla + precio.
        # El SKU se calcula en el server: codigo_producto-codigo_color-talla.nombre.
        user = request.user
        empresa = getattr(user, "empresa", None)

        serializer = ProductoVarianteOnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        producto = serializer.validated_data['producto']
        color = serializer.validated_data['color']
        talla = serializer.validated_data.get('talla')

        # Sin empresa (y sin ser superusuario) se rechaza: antes la guarda
        # ``empresa and ...`` se saltaba y la variante caía en la empresa ajena.
        if (empresa is None or producto.empresa_id != empresa.pk):
            raise ValidationError({"producto": "No pertenece a tu empresa."})

        if not producto.codigo:
            raise ValidationError({"producto": "El producto no tiene codigo asignado; no se puede generar el SKU."})

        # Solo Producto Terminado exige talla -- materia prima, compras, etc. no.
        es_producto_terminado = producto.tipo is not None and producto.tipo.codigo == CODIGO_PRODUCTO_TERMINADO
        if es_producto_terminado and talla is None:
            raise ValidationError({"talla": "Requerida para variantes de Producto Terminado."})

        if talla is not None and not talla_permitida_para_producto(producto, talla):
            raise ValidationError({"talla": "Esta talla no esta permitida para la categoria de este producto."})

        partes_sku = [producto.codigo, color.codigo] + ([talla.nombre] if talla else [])
        sku = "-".join(partes_sku).strip().upper()
        if len(sku) > 50:
            raise ValidationError({"sku": "El SKU generado excede el largo maximo permitido."})
        if ProductoVariante.objects.filter(empresa=producto.empresa, sku=sku).exists():
            raise ValidationError({"sku": f"El SKU '{sku}' ya existe."})

        variante = serializer.save(sku=sku, empresa=producto.empresa, talla=talla)
        return Response(ProductoVarianteSerializer(variante).data, status=201)


