from django.contrib.postgres.indexes import OpClass
from django.db import models
from django.db.models.functions import Upper
from nucleo.indices import GinIndexSoloPostgres, IndexSoloPostgres
from nucleo.models import Empresa, UnidadMedida, Impuesto, SatClaveProdServ, SatClaveUnidad, StatusLifecycleModel
from simple_history.models import HistoricalRecords

class TipoProducto(models.Model):
    codigo = models.CharField(max_length=50)

    history = HistoricalRecords()

    class Meta:
        db_table = "tipo_producto"
        verbose_name = "Tipo Producto"
        verbose_name_plural = "Tipos Producto"

    def __str__(self):
        return self.codigo

class CategoriaProducto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="categorias_producto")
    nombre = models.CharField(max_length=100)
    codigo = models.CharField(max_length=3)
    descripcion = models.CharField(max_length=150)
    unidad_medida = models.ForeignKey(
        UnidadMedida, on_delete=models.SET_NULL, related_name="categorias_producto", null=True, blank=True,
    )
    activo = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tallas = models.ManyToManyField(
        "Talla",
        through="CategoriaProductoTalla",
        related_name="categorias_producto",
        blank=True,
    )

    history = HistoricalRecords()

    class Meta:
        db_table = "categorias_producto"
        verbose_name = "Categoria Producto"
        verbose_name_plural = "Categorias Producto"

    def __str__(self):
        return self.nombre

class Color(models.Model):
    nombre = models.CharField(max_length=50)
    codigo = models.CharField(max_length=3)
    codigo_hex = models.CharField(max_length=7)
    pantone = models.CharField(max_length=20, blank=True, default="")
    activo = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        db_table = "colores"
        verbose_name = "Color"
        verbose_name_plural = "Colores"
    
    def __str__(self):
        return self.nombre

class Talla(models.Model):
    nombre = models.CharField(max_length=50)
    activo = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        db_table = "tallas"
        verbose_name = "Talla"
        verbose_name_plural = "Tallas"

    def __str__(self):
        return self.nombre

class CategoriaProductoTalla(models.Model):
    categoria_producto = models.ForeignKey(CategoriaProducto, on_delete=models.CASCADE, related_name="tallas_permitidas")
    talla = models.ForeignKey(Talla, on_delete=models.CASCADE, related_name="categorias_permitidas")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "categorias_producto_tallas"
        verbose_name = "Talla por Categoria"
        verbose_name_plural = "Tallas por Categoria"
        constraints = [
            models.UniqueConstraint(fields=["categoria_producto", "talla"], name="uq_categoria_producto_talla"),
        ]

    def __str__(self):
        return f"{self.categoria_producto.nombre} - {self.talla.nombre}"

class Producto(models.Model):
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="productos")
    categoria_producto = models.ForeignKey(CategoriaProducto, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    unidad_medida = models.ForeignKey(UnidadMedida, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    impuesto = models.ForeignKey(Impuesto, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    sat_prodserv = models.ForeignKey(SatClaveProdServ, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    sat_unidad = models.ForeignKey(SatClaveUnidad, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    nombre = models.CharField(max_length=100)
    descripcion = models.CharField(max_length=150, blank=True, default="")
    activo = models.BooleanField(default=True)
    tipo = models.ForeignKey(TipoProducto, on_delete=models.CASCADE, related_name="productos", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # campos para costo
    # costo_base = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    precio_base = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cod_proscai = models.CharField(max_length=50, blank=True, default="")
    codigo = models.CharField(max_length=5, null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        db_table = "productos"
        verbose_name = "Producto"
        verbose_name_plural = "Productos"
        indexes = [
            # Buscador global (``/api/v1/search/``). Índices de EXPRESIÓN sobre
            # ``UPPER(col)``: Django compila ``istartswith``/``icontains`` como
            # ``UPPER("col"::text) LIKE UPPER(%s)``, que ni un índice sobre la
            # columna cruda ni el ``_like`` de un campo ``unique`` pueden servir.
            # Mismo patrón que ``ventas.Pedido`` (ver ``nucleo/api/search.py``).
            # Sólo existen en PostgreSQL: ver
            # ``nucleo.indices``.
            # - ``nombre`` (NOMBRE, subcadena, vía ``producto__nombre``): GIN trigram.
            #   OJO: hoy el buscador sólo consulta este campo dentro de un OR que
            #   cruza ``variantes_producto`` y ``productos``, y ese OR no puede usar
            #   índices de una sola tabla (ver la entrada ``producto`` en
            #   ``nucleo/api/search.py``).
            GinIndexSoloPostgres(
                OpClass(Upper("nombre"), name="gin_trgm_ops"),
                name="productos_nombre_upper_trgm",
            ),
        ]
    
    def __str__(self):
        return self.nombre

class ProductoVariante(models.Model):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="variantes")
    nombre = models.CharField(max_length=150, blank=True, default="")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="variantes")
    color = models.ForeignKey(Color, on_delete=models.CASCADE, related_name="variantes")
    talla = models.ForeignKey(Talla, on_delete=models.CASCADE, related_name="variantes", null=True, blank=True)
    sku = models.CharField(max_length=50, unique=True)
    cod_proscai = models.CharField(max_length=50, blank=True, default="")
    precio_base = models.DecimalField(max_digits=10, decimal_places=2)
    activo = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        db_table = "variantes_producto"
        verbose_name = "Variante Producto"
        verbose_name_plural = "Variantes Producto"
        indexes = [
            # Buscador global (``/api/v1/search/``). Índices de EXPRESIÓN sobre
            # ``UPPER(col)``: Django compila ``istartswith``/``icontains`` como
            # ``UPPER("col"::text) LIKE UPPER(%s)``, que ni un índice sobre la
            # columna cruda ni el ``_like`` de un campo ``unique`` pueden servir.
            # Mismo patrón que ``ventas.Pedido`` (ver ``nucleo/api/search.py``).
            # Sólo existen en PostgreSQL: ver
            # ``nucleo.indices``.
            # - ``sku`` (CÓDIGO, prefijo): btree ``text_pattern_ops``.
            # - ``nombre`` (NOMBRE, subcadena): GIN trigram.
            IndexSoloPostgres(
                OpClass(Upper("sku"), name="text_pattern_ops"),
                name="variantes_sku_upper_like",
            ),
            GinIndexSoloPostgres(
                OpClass(Upper("nombre"), name="gin_trgm_ops"),
                name="variantes_nombre_upper_trgm",
            ),
        ]

    @property
    def nombre_completo(self):
        partes = [self.producto.nombre, self.color.nombre]
        if self.talla_id:
            partes.append(self.talla.nombre)
        return " - ".join(partes)

    def save(self, *args, **kwargs):
        self.nombre = self.nombre_completo
        super().save(*args, **kwargs)

    def __str__(self):
        return self.nombre_completo

class VarianteProductoProduccion(StatusLifecycleModel):
    """SKU de producción nacido de una muestra (``PedidoDetalle.producto_nombre_externo``),
    antes de que exista -- o sin que nunca llegue a existir -- su equivalente en el
    catálogo real (``catalogo.Producto``/``ProductoVariante``).

    Vive en tabla separada a propósito (ver ``DOCS/arquitectura/dbdiagram.io.md``,
    sección CATALOGO): un listado o búsqueda de catálogo NO debe traer muestras que
    tal vez nunca se vendan de nuevo. ``aplica_catalogo`` es el flag pendiente de
    "promover a catálogo real", para que mesa de control decida después -- no hay
    todavía una función que lo consuma.
    """
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="variantes_produccion")
    # Nullable: se crea desde el detalle del pedido especial, antes de que exista
    # una OP (``produccion.api.views.PedidoEspecialViewSet.variante_onboarding``).
    op = models.ForeignKey('produccion.OrdenProduccion', on_delete=models.CASCADE, null=True, blank=True, related_name="variantes_produccion")
    # Línea de muestra que originó este SKU -- de aquí sale el "ya existe SKU"
    # que consume ``PedidoEspecialDetailSerializer`` sin reconsultar nada más.
    pedido_detalle = models.ForeignKey('ventas.PedidoDetalle', on_delete=models.CASCADE, null=True, blank=True, related_name="variantes_produccion")
    producto_base = models.ForeignKey(Producto, on_delete=models.CASCADE, null=True, blank=True, related_name="variantes_produccion")
    color = models.ForeignKey(Color, on_delete=models.CASCADE, null=True, blank=True, related_name="variantes_produccion")
    talla = models.ForeignKey(Talla, on_delete=models.CASCADE, null=True, blank=True, related_name="variantes_produccion")

    nombre = models.CharField(max_length=150, blank=True, default="")
    sku = models.CharField(max_length=50, unique=True, null=True, blank=True)
    aplica_catalogo = models.BooleanField(default=False)

    history = HistoricalRecords()

    class Meta:
        db_table = "variantes_producto_produccion"
        verbose_name = "Variante Producto Produccion"
        verbose_name_plural = "Variantes Producto Produccion"
        constraints = [
            models.UniqueConstraint(
                fields=["pedido_detalle", "talla"],
                name="uq_variante_produccion_pedido_detalle_talla",
            )
        ]


