"""Generacion de Producto.codigo para el onboarding de alta de SKU.

Correlativo dentro de la categoria: prefijo = categoria_producto.codigo,
seguido de un consecutivo con ceros a la izquierda hasta llenar los 5
caracteres de Producto.codigo. Debe llamarse dentro de una transaccion
atomica que tambien haga el INSERT del Producto, para que el
``select_for_update`` sirva de algo contra altas concurrentes en la
misma categoria.
"""

from rest_framework.exceptions import ValidationError

from catalogo.models import Producto

CODIGO_PRODUCTO_MAX_LENGTH = 5


def siguiente_codigo_producto(categoria, lock=True):
    """``lock=True`` (default) para altas reales, dentro de una transaccion
    atomica que tambien haga el INSERT. ``lock=False`` para previsualizar en
    un GET de solo lectura -- el valor mostrado es orientativo, el real se
    recalcula con lock al momento de crear."""
    prefijo = (categoria.codigo or "").strip().upper()
    ancho = max(CODIGO_PRODUCTO_MAX_LENGTH - len(prefijo), 1)
    tope = 10 ** ancho

    qs = Producto.objects.filter(categoria_producto=categoria, codigo__startswith=prefijo)
    if lock:
        qs = qs.select_for_update()
    existentes = qs.values_list("codigo", flat=True)
    maximo = -1
    for codigo in existentes:
        sufijo = codigo[len(prefijo):]
        if sufijo.isdigit():
            maximo = max(maximo, int(sufijo))

    siguiente = maximo + 1
    if siguiente >= tope:
        raise ValidationError({
            "categoria_producto": f"Se agotaron los codigos disponibles para la categoria '{categoria.nombre}'.",
        })
    return f"{prefijo}{siguiente:0{ancho}d}"
