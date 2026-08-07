from django.db import migrations


class Migration(migrations.Migration):
    """Libera el candado de "una sola OB activa por pedido".

    Gemela de ``0025``, que ya quitó las constraints equivalentes de
    ``OrdenesCorteManga`` y ``OrdenesReflejante`` al habilitar parcialidades
    por renglón (``detalles_override[]``). ``OrdenesBordado`` quedó fuera de
    aquel commit, así que seguía respondiendo 409 en la segunda OB parcial del
    mismo pedido aunque el chequeo de Python ya la permitía.

    El control de "no programar más piezas de las contratadas" pasa a ser el
    cupo por línea de ``OrdenBordadoService`` (``ya_asignado + nuevo <=
    PedidoDetalleTalla.cantidad``); el 409 de pedido completo sigue vivo en
    ``buscar_existente_full_match`` para el POST sin override.
    """

    dependencies = [
        ('produccion', '0025_remove_ordenescortemanga_uq_orden_corte_manga_activa_por_pedido_and_more'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='ordenesbordado',
            name='uq_orden_bordado_activa_por_pedido',
        ),
    ]
