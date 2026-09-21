from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Lower
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from catalogo.tallas import talla_sort_key
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante
from catalogo.api.serializers import TipoProductoSerializer, CategoriaProductoSerializer, ColorSerializer, TallaSerializer, ProductoSerializer, ProductoOnboardingSerializer, ProductoVarianteSerializer
from produccion.models import ListaMaterialBom

class TipoProductoViewSet(viewsets.ModelViewSet):
    queryset = TipoProducto.objects.all()
    serializer_class = TipoProductoSerializer

class CategoriaProductoViewSet(viewsets.ModelViewSet):
    serializer_class = CategoriaProductoSerializer

    def get_queryset(self):
        return CategoriaProducto.objects.filter(activo=True).order_by("-created_at", "-id")

class ColorViewSet(viewsets.ModelViewSet):
    serializer_class = ColorSerializer

    def get_queryset(self):
        return Color.objects.filter(activo=True).order_by(Lower("nombre"), "id")

class TallaViewSet(viewsets.ModelViewSet):
    serializer_class = TallaSerializer

    def get_queryset(self):
        return Talla.objects.filter(activo=True)

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
        qs = super().get_queryset()
        tipo_id = self.request.query_params.get('tipo_id')
        if tipo_id is not None:
            try:
                tipo_id = int(tipo_id)
            except (TypeError, ValueError):
                raise ValidationError({"tipo_id": "Must be an integer."})
            qs = qs.filter(tipo_id=tipo_id)
        q = (self.request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(nombre__icontains=q)
                | Q(codigo__icontains=q)
                | Q(cod_proscai__icontains=q)
            )
        return qs.order_by("-created_at", "-id")

    def perform_create(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        if not getattr(user, "is_superuser", False) and empresa:
            serializer.save(empresa=empresa)
            return
        serializer.save()

    @action(detail=False, methods=['post'])
    def onboarding(self, request):
        # Alta simplificada para producción: nombre + tipo + categoria + precio, sin descripcion.
        user = request.user
        empresa = getattr(user, "empresa", None)
        is_superuser = getattr(user, "is_superuser", False)

        serializer = ProductoOnboardingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        categoria = serializer.validated_data['categoria_producto']
        if not is_superuser and empresa and categoria.empresa_id != empresa.pk:
            raise ValidationError({"categoria_producto": "No pertenece a tu empresa."})

        if not is_superuser and empresa:
            producto = serializer.save(empresa=empresa)
        else:
            producto = serializer.save()
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
        qs = super().get_queryset()
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


