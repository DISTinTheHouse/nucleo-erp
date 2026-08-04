"""Helpers compartidos por los tres services de órdenes de trabajo
(Bordado / Reflejante / Corte de Manga), que son estructuralmente paralelos.

Centraliza lo que antes estaba triplicado verbatim en cada service:

- ``revisar_empresa``: núcleo del chequeo de pertenencia a empresa (lo envuelven
  ``_validar_contexto`` en los services y los serializers satélite, cada uno con
  su propia forma de error).
- ``tallas_orden_trabajo_qs``: criterio único de tallas que entran a la orden.
- ``payload_duplicada``: payload 409 de orden duplicada.
- ``crear_orden_con_guardia_duplicado``: creación con traducción de
  ``IntegrityError`` (violación de la constraint parcial) a ese 409, sin depender
  del texto del error —que varía por backend—.
"""

from django.db import IntegrityError, transaction

from ventas.models import PedidoDetalleTalla


def revisar_empresa(user, obj):
    """Compara la empresa de ``obj`` contra la del ``user``.

    Devuelve ``None`` si coinciden, ``"sin_empresa"`` si el usuario no tiene
    empresa asignada, o ``"otra_empresa"`` si difieren. Sólo clasifica y no
    lanza: cada llamador arma el error con su convención (string plano en los
    services vía ``_validar_contexto``, dict por campo en los serializers
    satélite) y con sus propios mensajes.
    """
    empresa = getattr(user, "empresa", None)
    if empresa is None:
        return "sin_empresa"
    if obj.empresa_id != empresa.pk:
        return "otra_empresa"
    return None


def tallas_orden_trabajo_qs(pedido_id, lleva_field):
    """Tallas del pedido que entran a una orden de trabajo: marcadas con
    ``lleva_field`` y con cantidad real a producir (``cantidad > 0``).

    Fuente única consumida tanto por ``buscar_existente_full_match`` (contar lo
    esperado) como por ``save`` (crear el detalle); que divergieran era el bug
    del conteo que permitía esquivar el 409 con una talla en cantidad 0. Se
    excluye ``cantidad=0`` porque un renglón por cero piezas no es trabajo de
    producción; mismo criterio que el picking (``cantidad_validator``) y que la
    generación desde ventas.
    """
    return PedidoDetalleTalla.objects.filter(
        pedido_detalle__pedido_id=pedido_id,
        cantidad__gt=0,
        **{lleva_field: True},
    )


def payload_duplicada(existente, *, folio_field, estatus_display, estatus_field,
                      payload_key, tipo_label, dividir_label):
    """Payload del 409 de orden duplicada, uniforme para los tres tipos
    (difieren sólo en el campo de folio/estatus y en las etiquetas de texto)."""
    display = getattr(existente, estatus_display, None)
    return {
        "err": (
            f"Ya existe una orden de {tipo_label} activa para este pedido con el 100% "
            f"de las prendas. Si requiere dividir {dividir_label}, contacte a producción."
        ),
        payload_key: {
            "id": existente.id,
            "folio": getattr(existente, folio_field),
            "pedido": existente.pedido_id,
            "estado": display() if display else getattr(existente, estatus_field),
        },
    }


def crear_orden_con_guardia_duplicado(modelo, pedido, crear_kwargs,
                                      duplicada_exc, payload_builder):
    """Crea la orden en un savepoint propio y traduce la violación de la
    constraint parcial ``uq_orden_*_activa_por_pedido`` en la excepción 409 del
    módulo.

    **No** inspecciona el texto del ``IntegrityError`` (que varía por backend:
    PostgreSQL incrusta el nombre de la constraint, SQLite no): tras capturarlo,
    re-consulta si el pedido ya tiene una orden activa. Si la hay, es el
    duplicado (carrera entre el chequeo previo y este INSERT) y responde 409; si
    no, re-lanza el error real. El savepoint evita que el ``IntegrityError``
    deje inutilizable la transacción envolvente antes de esa re-consulta.
    """
    try:
        with transaction.atomic():
            return modelo.objects.create(**crear_kwargs)
    except IntegrityError:
        existente = (
            modelo.objects.filter(pedido=pedido, activo=True).order_by("-id").first()
        )
        if existente is None:
            raise
        raise duplicada_exc(payload_builder(existente))
