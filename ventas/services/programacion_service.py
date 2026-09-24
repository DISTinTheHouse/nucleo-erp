from ventas.models import PedidoDetalleTalla

# EMBARQUE/APARTADO son logística genérica: cualquier pedido puede
# embarcarse o apartarse, sin importar qué servicios lleve. BORDADO/
# REFLEJANTE/CORTE_MANGA sólo aplican si el pedido tiene al menos una talla
# marcada con ese servicio -- si no, no tiene sentido programarlo ahí.
_DESTINOS_LOGISTICA = ("EMBARQUE", "APARTADO")
_DESTINOS_POR_SERVICIO = (
    ("BORDADO", "lleva_bordado"),
    ("REFLEJANTE", "lleva_reflejante"),
    ("CORTE_MANGA", "lleva_corte_manga"),
)


def destinos_aplicables(pedido):
    """Destinos de ``PATCH /pedidos/{id}/programar/`` que tiene sentido
    ofrecer para ESTE pedido, según qué servicios llevan sus tallas.

    Evita que mesa de control programe un servicio que el pedido no pidió
    (ej. CORTE_MANGA en un pedido sin ninguna talla de corte de manga).
    """
    tallas = PedidoDetalleTalla.objects.filter(pedido_detalle__pedido=pedido)
    destinos = [
        destino
        for destino, campo in _DESTINOS_POR_SERVICIO
        if tallas.filter(**{campo: True}).exists()
    ]
    destinos.extend(_DESTINOS_LOGISTICA)
    return destinos
