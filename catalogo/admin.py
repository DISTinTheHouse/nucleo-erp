from django.contrib import admin
from catalogo.models import TipoProducto, CategoriaProducto, Color, Talla, Producto, ProductoVariante

admin.site.register(TipoProducto) 
admin.site.register(CategoriaProducto)
admin.site.register(Color)
admin.site.register(Talla)
admin.site.register(Producto)
admin.site.register(ProductoVariante)
