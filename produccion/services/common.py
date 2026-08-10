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
from django.db.models import Sum

from ventas.models import PedidoDetalleTalla

#: Tolerancia al comparar cantidades. Los ``cantidad`` del detalle de las
#: órdenes son ``FloatField``, así que sumar varias parcialidades puede dejar
#: residuos del orden de 1e-15 que no representan piezas reales.
EPS_CANTIDAD = 1e-9


def cantidades_asignadas(detalle_model, fk_orden, pedido_ids):
    """Lo ya programado en órdenes activas, para varios pedidos en una query.

    ``fk_orden`` es el nombre de la FK a la orden padre en el modelo de detalle
    (``ob`` / ``orden_r`` / ``ocm``). Devuelve la tupla
    ``(por_linea, sin_talla)``:

    - ``por_linea``: ``{(pedido_detalle_id, talla_id): cantidad}`` para los
      renglones con talla identificada.
    - ``sin_talla``: ``{pedido_detalle_id: cantidad}`` con los renglones cuyo
      ``talla_id`` es ``NULL``. La FK ``talla`` del detalle es ``SET_NULL`` y
      el pipeline de picking escribe ``NULL`` cuando la ``PedidoDetalleTalla``
      no trae ``variante`` (``wms/services/picking_pipeline/work_orders.py``),
      así que esas piezas existen y consumen cupo. Antes caían en una clave
      ``(pd_id, None)`` que ningún lookup buscaba y contaban como cero.

    La clave ``(pedido_detalle_id, talla_id)`` no colisiona entre pedidos —un
    ``PedidoDetalle`` pertenece a un único pedido— así que el dict combinado es
    seguro. Sólo cuenta órdenes ``activo=True``: las canceladas o dadas de baja
    no consumen cupo.
    """
    filas = (
        detalle_model.objects
        .filter(**{
            f"{fk_orden}__pedido_id__in": list(pedido_ids),
            f"{fk_orden}__activo": True,
        })
        .values("pedido_detalle_id", "talla_id")
        .annotate(asignado=Sum("cantidad"))
    )
    por_linea = {}
    sin_talla = {}
    for f in filas:
        cantidad = float(f["asignado"] or 0)
        if f["talla_id"] is None:
            sin_talla[f["pedido_detalle_id"]] = (
                sin_talla.get(f["pedido_detalle_id"], 0.0) + cantidad
            )
        else:
            por_linea[(f["pedido_detalle_id"], f["talla_id"])] = cantidad
    return por_linea, sin_talla


def pendientes_por_linea(lineas, por_linea, sin_talla):
    """Calcula ``(asignada, pendiente)`` por línea aplicando el pool sin talla.

    ``lineas`` es ``[(pedido_detalle_id, talla_id, cantidad_pedido), ...]``.
    Las piezas de ``sin_talla`` no se pueden atribuir a una talla concreta, así
    que se drenan en orden contra las líneas del mismo ``pedido_detalle``: el
    pendiente por línea puede quedar repartido de forma distinta a la real,
    pero el **total** por ``pedido_detalle`` sí queda correcto, que es lo que
    evita ofrecer cantidad que ya está programada.
    """
    pool = dict(sin_talla)
    resultado = []
    for pedido_detalle_id, talla_id, cantidad_pedido in lineas:
        asignada = por_linea.get((pedido_detalle_id, talla_id), 0.0)
        pendiente = max(0.0, cantidad_pedido - asignada)
        disponible_pool = pool.get(pedido_detalle_id, 0.0)
        if disponible_pool > 0 and pendiente > 0:
            consumido = min(disponible_pool, pendiente)
            asignada += consumido
            pendiente -= consumido
            pool[pedido_detalle_id] = disponible_pool - consumido
        if pendiente <= EPS_CANTIDAD:
            pendiente = 0.0
        resultado.append((asignada, pendiente))
    return resultado


def config_como_dict(valor):
    """Normaliza un ``*_config`` de ``PedidoDetalleTalla`` a dict.

    Los tres campos de configuración (``bordado_config`` /
    ``reflejante_config`` / ``corte_manga_config``) son ``JSONField`` libres, y
    **no comparten forma** en datos reales:

    - ``bordado_config``: objeto ``{"ubicaciones": [...], "notas": ...}``.
    - ``corte_manga_config``: objeto ``{"tipo": ...}``.
    - ``reflejante_config``: **ARREGLO** ``[{"tipo", "opcion", "posicion"}, ...]``.

    Todo el código que lee estos campos fue escrito contra la forma de bordado
    (``cfg.get(...)``), así que un ``reflejante_config`` revienta con
    ``AttributeError: 'list' object has no attribute 'get'``. Ese mismo fallo ya
    causó tres 500 distintos en este proyecto (lectura del retrieve, escritura
    de ``save()`` y el GET de onboarding), porque cada arreglo se hizo local en
    vez de compartido. Este helper es el punto único de normalización.

    Devuelve ``{}`` para cualquier cosa que no sea dict —incluido el arreglo de
    reflejante—. **No** traduce el arreglo a la forma de bordado: sus elementos
    describen material y colocación (``tipo``/``opcion``/``posicion``), no
    estampados con imagen y medidas; mapear unos a otros devolvería datos
    plausibles pero equivocados. Tampoco toma ``valor[0]``: hay filas reales con
    2 y 3 elementos de ``posicion`` distinta, así que quedarse con el primero
    perdería posiciones en silencio.
    """
    return valor if isinstance(valor, dict) else {}


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


def _tiene_candado_activa_por_pedido(modelo):
    """¿El modelo declara la constraint parcial "una orden activa por pedido"?

    Se lee del ``Meta`` en vez de hardcodear la respuesta para que vuelva a ser
    verdad sola si negocio re-instaura el candado en alguno de los tres tipos.
    """
    return any(
        "activa_por_pedido" in getattr(constraint, "name", "")
        for constraint in modelo._meta.constraints
    )


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
        # Sólo se traduce si el modelo **todavía** declara el candado de "una
        # orden activa por pedido". Al quitarlo (migraciones ``0025`` para
        # Reflejante/OCM y ``0026`` para Bordado) el único índice único que
        # queda es el del folio, y como ahora es normal que un pedido tenga
        # órdenes activas, la re-consulta encontraba una y reportaba un 409 de
        # duplicado sobre lo que en realidad era una colisión de folio
        # —enmascarando la causa real—.
        if not _tiene_candado_activa_por_pedido(modelo):
            raise
        existente = (
            modelo.objects.filter(pedido=pedido, activo=True).order_by("-id").first()
        )
        if existente is None:
            raise
        raise duplicada_exc(payload_builder(existente))
