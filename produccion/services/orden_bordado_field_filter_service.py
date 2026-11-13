"""Reexporta el pedido vinculado y su cliente (EC-880) desde ``ventas``.

La implementación vive en ``ventas.services.pedido_vinculado`` desde EC-882,
que la comparte con WMS; esta ruta se conserva para las importaciones de
Producción.
"""

from ventas.services.pedido_vinculado import (  # noqa: F401
    _CLIENTE_SNAPSHOT,
    armar_pedido_vinculado,
    cliente_de_pedido,
)
