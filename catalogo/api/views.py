from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Lower
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response
from catalogo.codigos import siguiente_codigo_producto
from catalogo.tallas import talla_sort_key
from catalogo.validaciones import talla_permitida_para_producto
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante, CategoriaProductoProceso
from catalogo.api.serializers import REGLA_DUPLICADA, CategoriaProductoProcesoSerializer, TipoProductoSerializer, CategoriaProductoSerializer, ColorSerializer, TallaSerializer, ProductoSerializer, ProductoOnboardingSerializer, ProductoVarianteSerializer, ProductoVarianteOnboardingSerializer
from produccion.models import ListaMaterialBom

# Solo Producto Terminado exige talla en el onboarding de variante; materia
# prima, compras, etc. no tienen tallas. Mismo id que usa el picker del
# frontend (useProductVariantForm.ts) para filtrar el selector de producto.
TIPO_PRODUCTO_TERMINADO_ID = 3


def _alcance_empresa(qs, user):
    # Aislamiento multi-tenant: superusuario ve todo, el resto sólo su empresa y
    # quien no tiene empresa no ve nada. Detalle ajeno -> 404, listado -> 200 [].
    if getattr(user, "is_superuser", False):
        return qs
    empresa = getattr(user, "empresa", None)
    if empresa:
        return qs.filter(empresa=empresa)
    return qs.none()


class TipoProductoViewSet(viewsets.ModelViewSet):
    queryset = TipoProducto.objects.all()
    serializer_class = TipoProductoSerializer

class CategoriaProductoViewSet(viewsets.ModelViewSet):
    serializer_class = CategoriaProductoSerializer

    def get_queryset(self):
        qs = CategoriaProducto.objects.filter(activo=True).order_by("-created_at", "-id")
        return _alcance_empresa(qs, self.request.user)

class SoloAdminEmpresaEscribe(BasePermission):
    """Todos leen; solo el superusuario o el admin de empresa escriben (EC-575)."""

    def has_permission(self, request, view):
        user = request.user
        return request.method in SAFE_METHODS or bool(
            getattr(user, "is_superuser", False) or getattr(user, "is_admin_empresa", False)
        )


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
        except IntegrityError:
            raise ValidationError({"non_field_errors": [REGLA_DUPLICADA]})

    def perform_destroy(self, instance):
        instance.activo = False
        instance.save(update_fields=["activo", "updated_at"])


class ColorViewSet(viewsets.ModelViewSet):
    serializer_class = ColorSerializer

    def get_queryset(self):
        return Color.objects.filter(activo=True).order_by(Lower("nombre"), "id")

class SoloSuperusuarioEscribe(BasePermission):
    """Catálogo global (sin empresa): todos leen, solo el superusuario escribe (#535)."""

    def has_permission(self, request, view):
        return request.method in SAFE_METHODS or request.user.is_superuser


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
            qs = qs.filter(categorias_producto__id=categoria_id).distinct()
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

class ProductoViewSet(viewsets.ModelViewSet):
    queryset = Producto.objects.all()
    serializer_class = ProductoSerializer

    def get_queryset(self):
        qs = _alcance_empresa(super().get_queryset().filter(activo=True), self.request.user)
        # ``tipo_id`` acepta uno o varios: repetido (``?tipo_id=1&tipo_id=3``) o
        # separado por comas (``?tipo_id=1,3``). Un solo valor filtra igual que antes.
        tipo_ids = self.request.query_params.getlist('tipo_id')
        if tipo_ids:
            try:
                tipo_ids = [int(valor) for crudo in tipo_ids for valor in crudo.split(',')]
            except (TypeError, ValueError):
                raise ValidationError({"tipo_id": "Must be an integer."})
            qs = qs.filter(tipo_id__in=tipo_ids)
        q = (self.request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(nombre__icontains=q)
                | Q(codigo__icontains=q)
                | Q(cod_proscai__icontains=q)
            )
        return qs.order_by("-created_at", "-id")

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
        if not getattr(user, "is_superuser", False) and empresa:
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
        is_superuser = getattr(user, "is_superuser", False)
        # Sin empresa (y sin ser superusuario) se rechaza, no se salta la guarda.
        if not is_superuser and (empresa is None or categoria.empresa_id != empresa.pk):
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
        is_superuser = getattr(user, "is_superuser", False)

        serializer = ProductoOnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        categoria = serializer.validated_data['categoria_producto']
        # Sin empresa (y sin ser superusuario) se rechaza: antes la guarda
        # ``empresa and ...`` se saltaba y el producto caía en la empresa ajena.
        if not is_superuser and (empresa is None or categoria.empresa_id != empresa.pk):
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

class ProductoVarianteViewSet(viewsets.ModelViewSet):
    # producto_nombre/color_nombre/talla_nombre (agregados en 0599352) recorren las FK
    # producto, color y talla por su atributo .nombre. Con un queryset .all() sin
    # select_related, DRF resuelve esas relaciones de forma perezosa fila por fila: el
    # listado completo dispara 3 consultas por variante (N+1) y sobre la tabla real de
    # variantes el endpoint tarda tanto que parece colgarse. select_related las trae en el
    # mismo JOIN -> una sola consulta, sin alterar la forma de la respuesta.
    queryset = ProductoVariante.objects.all().select_related("producto", "color", "talla")
    serializer_class = ProductoVarianteSerializer

    def get_queryset(self):
        # ``ProductoVariante.empresa`` es FK propia (el onboarding la copia de
        # ``producto.empresa``); se acota por ella, no por ``producto__empresa``.
        qs = _alcance_empresa(super().get_queryset(), self.request.user)
        if self.request.query_params.get('con_bom', '').lower() == 'true':
            bom_qs = ListaMaterialBom.objects.filter(
                producto_variante=OuterRef('pk'),
                activo=True,
            )
            empresa = getattr(self.request.user, 'empresa', None)
            if empresa:
                bom_qs = bom_qs.filter(empresa=empresa)
            qs = qs.filter(Exists(bom_qs))
        q = (self.request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(sku__icontains=q)
                | Q(cod_proscai__icontains=q)
                | Q(producto__cod_proscai__icontains=q)
                | Q(nombre__icontains=q)
                | Q(producto__nombre__icontains=q)
            )
        return qs

    @action(detail=False, methods=['post'])
    def onboarding(self, request):
        # Alta simplificada de variante: producto + color + talla + precio.
        # El SKU se calcula en el server: codigo_producto-codigo_color-talla.nombre.
        user = request.user
        empresa = getattr(user, "empresa", None)
        is_superuser = getattr(user, "is_superuser", False)

        serializer = ProductoVarianteOnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        producto = serializer.validated_data['producto']
        color = serializer.validated_data['color']
        talla = serializer.validated_data.get('talla')

        # Sin empresa (y sin ser superusuario) se rechaza: antes la guarda
        # ``empresa and ...`` se saltaba y la variante caía en la empresa ajena.
        if not is_superuser and (empresa is None or producto.empresa_id != empresa.pk):
            raise ValidationError({"producto": "No pertenece a tu empresa."})

        if not producto.codigo:
            raise ValidationError({"producto": "El producto no tiene codigo asignado; no se puede generar el SKU."})

        # Solo Producto Terminado exige talla -- materia prima, compras, etc. no.
        es_producto_terminado = producto.tipo_id == TIPO_PRODUCTO_TERMINADO_ID
        if es_producto_terminado and talla is None:
            raise ValidationError({"talla": "Requerida para variantes de Producto Terminado."})

        if talla is not None and not talla_permitida_para_producto(producto, talla):
            raise ValidationError({"talla": "Esta talla no esta permitida para la categoria de este producto."})

        partes_sku = [producto.codigo, color.codigo] + ([talla.nombre] if talla else [])
        sku = "-".join(partes_sku).strip().upper()
        if len(sku) > 50:
            raise ValidationError({"sku": "El SKU generado excede el largo maximo permitido."})
        if ProductoVariante.objects.filter(sku=sku).exists():
            raise ValidationError({"sku": f"El SKU '{sku}' ya existe."})

        variante = serializer.save(sku=sku, empresa=producto.empresa, talla=talla)
        return Response(ProductoVarianteSerializer(variante).data, status=201)


