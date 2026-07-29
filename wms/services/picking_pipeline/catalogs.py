from django.utils import timezone
from rest_framework.exceptions import ValidationError

from inventarios.models import Almacen
from nucleo.models import SerieFolio
from ventas.models import Pedido


def folio_preview(empresa, sucursal, tipo_documento="Picking"):
    """Preview del siguiente folio de ``SerieFolio`` sin persistir.

    Reutiliza ``SerieFolio.get_siguiente_folio()`` para coincidir con el
    formato real (serie, relleno_ceros, separador, incluir_anio, reinicios
    anuales, rangos). Si no existe la serie o falla el cálculo, devuelve
    ``None`` (no es bloqueante para el GET onboarding).
    """
    serie_folio = (
        SerieFolio.objects.filter(
            empresa=empresa,
            sucursal=sucursal,
            tipo_documento__iexact=tipo_documento,
            activo=True,
        )
        .order_by("id_serie_folio")
        .only(
            "serie",
            "folio_actual",
            "prefijo",
            "sufijo",
            "relleno_ceros",
            "separador",
            "incluir_anio",
            "reiniciar_anual",
            "ultimo_anio",
            "folio_inicial",
            "folio_final",
        )
        .first()
    )
    if not serie_folio:
        return None
    try:
        folio_formateado, _, _ = serie_folio.get_siguiente_folio()
        return folio_formateado
    except Exception:
        return None


def resolver_apartados_safe(empresa_id, sucursal_id):
    """GET onboarding: busca el almacén ``APARTADOS`` (iexact) sin fallar.

    Devuelve el ``Almacen`` o ``None`` si no está configurado. Usado como
    sugerencia inicial de ``almacen_destino``. Para el POST ver
    ``resolver_apartados_obligatorio``.
    """
    return (
        Almacen.objects.filter(
            nombre__iexact="APARTADOS",
            empresa_id=empresa_id,
            sucursal_id=sucursal_id,
        )
        .order_by("pk")
        .first()
    )


def resolver_apartados_obligatorio(pedido):
    """POST: busca el almacén ``APARTADOS`` (iexact) y lanza 400 si falta.

    Mantiene el criterio ``iexact`` alineado con el GET onboarding y con
    la data migration que siembra el catálogo de almacenes.
    """
    almacen_apartados = resolver_apartados_safe(pedido.empresa_id, pedido.sucursal_id)
    if not almacen_apartados:
        raise ValidationError(
            "No existe el almacén APARTADOS para la empresa y sucursal del pedido."
        )
    return almacen_apartados


def armar_payload_vacio():
    """Esqueleto del GET onboarding cuando no hay contexto suficiente.

    El ``PickingService.onboarding_payload`` lo devuelve directamente si
    faltan ``empresa`` (usuario sin asignar) o ``pedido_id`` (primer paso
    del flujo). Debe tener el mismo shape que el payload completo para que
    Next.js no dependa de si el backend sugiere datos o no.
    """
    return {
        "pedidos": [],
        "operadores": [],
        "almacenes": [],
        "almacen_origen": None,
        "almacen_destino": None,
        "header": {
            "fecha_picking_sugerida": None,
            "folio_sugerido_preview": None,
        },
        "pedido": None,
        "picking_detalle": [],
    }


def cargar_catalogos(user, empresa, es_staff, sucursal_ids):
    """Devuelve (pedidos, operadores, almacenes_qs, almacenes_payload).

    Se extrajo como función compartida porque el GET onboarding necesita
    los tres catálogos y además re-filtra ``almacenes_qs`` más adelante
    para sugerir el origen/destino sin correr una segunda consulta.
    """
    pedido_qs = (
        Pedido.objects.filter(
            empresa=empresa,
            activo=True,
            estatus__in=[3, 4],
        )
        .select_related("cliente", "sucursal")
        .order_by("-id")
    )
    if not es_staff:
        pedido_qs = pedido_qs.filter(sucursal_id__in=sucursal_ids)

    pedidos_payload = [
        {
            "id": pedido.id,
            "folio": pedido.folio,
            "cliente": pedido.cliente_id,
            "cliente_nombre": getattr(pedido.cliente, "nombre", None),
            "sucursal": pedido.sucursal_id,
            "sucursal_nombre": getattr(pedido.sucursal, "nombre", None),
        }
        for pedido in pedido_qs[:50]
    ]

    operadores_qs = (
        user.__class__.objects.filter(empresa=empresa, is_active=True)
        .order_by("first_name", "last_name", "email")
        .only("id", "first_name", "last_name", "email")
    )
    if not es_staff:
        operadores_qs = operadores_qs.filter(sucursal_default_id__in=sucursal_ids)

    operadores_payload = [
        {
            "id": operador.id,
            "nombre": operador.get_full_name().strip() or operador.email,
        }
        for operador in operadores_qs[:100]
    ]

    almacenes_qs = Almacen.objects.filter(empresa=empresa).order_by("codigo")
    if not es_staff:
        almacenes_qs = almacenes_qs.filter(sucursal_id__in=sucursal_ids)
    almacenes_payload = [
        {
            "id": almacen.pk,
            "codigo": almacen.codigo,
            "nombre": almacen.nombre,
            "sucursal": almacen.sucursal_id,
        }
        for almacen in almacenes_qs[:100]
    ]

    return pedido_qs, pedidos_payload, operadores_payload, almacenes_qs, almacenes_payload


def serializar_almacen(almacen):
    """Serializer ``dict`` inline para almacenes sugeridos (origen/destino)."""
    if almacen is None:
        return None
    return {
        "id": almacen.pk,
        "codigo": almacen.codigo,
        "nombre": almacen.nombre,
        "sucursal": almacen.sucursal_id,
    }


def sugerir_almacenes(pedido, almacen_origen_actual, almacen_destino_actual):
    """Sugiere almacén origen (menor pk != destino) y destino = APARTADOS.

    Regla del GET onboarding: no exponer como sugerencia un ``origen`` igual
    al ``destino`` (APARTADOS), porque el POST rechaza ese caso con 400.
    """
    origen_sugerido = almacen_origen_actual
    destino_sugerido = almacen_destino_actual

    if destino_sugerido is None:
        destino_sugerido = resolver_apartados_safe(pedido.empresa_id, pedido.sucursal_id)

    if origen_sugerido is None:
        exclude_pks = []
        if destino_sugerido is not None:
            exclude_pks.append(destino_sugerido.pk)
        origen_sugerido = (
            Almacen.objects.filter(
                empresa_id=pedido.empresa_id,
                sucursal_id=pedido.sucursal_id,
            )
            .exclude(pk__in=exclude_pks)
            .order_by("pk")
            .first()
        )
        if origen_sugerido is None and destino_sugerido is not None:
            origen_sugerido = (
                Almacen.objects.filter(
                    empresa_id=pedido.empresa_id,
                    sucursal_id=pedido.sucursal_id,
                )
                .order_by("pk")
                .first()
            )

    return origen_sugerido, destino_sugerido


def armar_header_preview(pedido):
    """Encabezado del onboarding: fecha sugerida + preview del folio picking."""
    fecha_picking_sugerida = timezone.now()
    folio_sugerido_preview = folio_preview(pedido.empresa, pedido.sucursal)
    return {
        "fecha_picking_sugerida": fecha_picking_sugerida.isoformat(),
        "folio_sugerido_preview": folio_sugerido_preview,
    }
