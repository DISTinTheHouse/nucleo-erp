from rest_framework import serializers
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante
from catalogo.validaciones import talla_permitida_para_producto
from finanzas.api.serializers import EmpresaResueltaEnServidorMixin


class EmpresaValidadaEnCreateMixin:
    """``empresa`` no se reasigna por update y en create debe ser la del usuario.

    Para los ViewSets SIN ``perform_create`` que inyecte la empresa (categoría y
    variante): el cliente la sigue mandando en el POST, así que no puede volverse
    de sólo lectura en create sin romper el alta (FK NOT NULL -> IntegrityError).

    - **Update (PUT/PATCH): de sólo lectura para TODOS**, superusuario incluido,
      igual que ``EmpresaResueltaEnServidorMixin``.
    - **Create: se valida** contra ``user.empresa``; el superusuario puede mandar
      cualquiera.

    ``empresa`` sigue apareciendo en la RESPUESTA: es un cambio de escritura, no
    de shape.
    """

    def get_extra_kwargs(self):
        extra_kwargs = super().get_extra_kwargs()
        if self.instance is not None:
            kwargs = dict(extra_kwargs.get("empresa", {}))
            kwargs["read_only"] = True
            # ``read_only`` y ``required`` son incompatibles en DRF.
            kwargs.pop("required", None)
            extra_kwargs["empresa"] = kwargs
        return extra_kwargs

    def validate_empresa(self, empresa):
        user = getattr(self.context.get("request"), "user", None)
        if getattr(user, "is_superuser", False):
            return empresa
        empresa_usuario = getattr(user, "empresa", None)
        if empresa_usuario is None or empresa.pk != empresa_usuario.pk:
            raise serializers.ValidationError("La empresa no corresponde a la empresa del usuario.")
        return empresa


class TipoProductoSerializer(serializers.ModelSerializer):
    class Meta:
        model = TipoProducto
        fields = '__all__'

class CategoriaProductoSerializer(EmpresaValidadaEnCreateMixin, serializers.ModelSerializer):
    class Meta:
        model = CategoriaProducto
        exclude = ['activo', 'created_at', 'updated_at']

class ColorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Color
        exclude = ['activo']

class TallaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Talla
        exclude = ['activo']

class ProductoSerializer(EmpresaResueltaEnServidorMixin, serializers.ModelSerializer):
    # ``perform_create`` de ``ProductoViewSet`` ya inyecta ``user.empresa``, que es
    # justo el contrato de ``EmpresaResueltaEnServidorMixin``: sólo lectura en update
    # para todos; en create sólo la manda el superusuario.
    class Meta:
        model = Producto
        fields = '__all__'
        extra_kwargs = {
            'empresa': {'required': False},
        }

class ProductoOnboardingSerializer(serializers.ModelSerializer):
    """Alta simplificada de Producto para producción: nombre + tipo + categoria + precio, sin descripcion."""
    tipo = serializers.PrimaryKeyRelatedField(queryset=TipoProducto.objects.all())
    categoria_producto = serializers.PrimaryKeyRelatedField(queryset=CategoriaProducto.objects.all())
    precio_base = serializers.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        model = Producto
        fields = ['id', 'nombre', 'tipo', 'categoria_producto', 'precio_base']

class ProductoVarianteOnboardingSerializer(serializers.ModelSerializer):
    """Alta simplificada de ProductoVariante: producto + color + talla + precio, el SKU se calcula en el server.
    talla es opcional: materia prima (tipo != Producto Terminado) no la exige, ver onboarding() en views.py."""
    producto = serializers.PrimaryKeyRelatedField(queryset=Producto.objects.all())
    color = serializers.PrimaryKeyRelatedField(queryset=Color.objects.all())
    talla = serializers.PrimaryKeyRelatedField(queryset=Talla.objects.all(), required=False, allow_null=True)
    precio_base = serializers.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        model = ProductoVariante
        fields = ['id', 'producto', 'color', 'talla', 'precio_base', 'sku']
        read_only_fields = ['sku']

class ProductoVarianteSerializer(EmpresaValidadaEnCreateMixin, serializers.ModelSerializer):
    producto_nombre = serializers.CharField(source='producto.nombre', read_only=True)
    color_nombre = serializers.CharField(source='color.nombre', read_only=True)
    talla_nombre = serializers.CharField(source='talla.nombre', read_only=True)
    cod_proscai = serializers.CharField(source='producto.cod_proscai', read_only=True)

    class Meta:
        model = ProductoVariante
        fields = '__all__'

    def validate(self, attrs):
        producto = attrs.get('producto') or getattr(self.instance, 'producto', None)
        # Aislamiento multi-tenant: el producto debe ser de la empresa de la
        # variante, para TODOS (superusuario incluido). En update ``empresa`` es
        # de sólo lectura, así que se resuelve de la instancia.
        empresa_id = attrs['empresa'].pk if 'empresa' in attrs else getattr(self.instance, 'empresa_id', None)
        if producto is not None and producto.empresa_id != empresa_id:
            raise serializers.ValidationError(
                {"producto": "El producto no pertenece a la empresa de la variante."}
            )
        talla = attrs['talla'] if 'talla' in attrs else getattr(self.instance, 'talla', None)
        if producto and talla is not None and not talla_permitida_para_producto(producto, talla):
            raise serializers.ValidationError(
                {"talla": "Esta talla no esta permitida para la categoria de este producto."}
            )
        return attrs
