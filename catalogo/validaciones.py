"""Validaciones de negocio de catalogo compartidas entre el onboarding y el CRUD completo."""

from catalogo.models import CategoriaProductoTalla


def talla_permitida_para_producto(producto, talla):
    """True si ``talla`` es valida para la categoria de ``producto``.

    Sin categoria en el producto no hay restriccion (True). Con categoria,
    la talla debe estar en ``CategoriaProductoTalla`` para esa categoria.
    """
    categoria = producto.categoria_producto
    if categoria is None:
        return True
    return CategoriaProductoTalla.objects.filter(categoria_producto=categoria, talla=talla).exists()
