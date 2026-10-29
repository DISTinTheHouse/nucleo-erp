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


# Destino de la programación -> almacén por nombre (mismo criterio que APARTADOS).
# EMBARQUE/REFLEJANTE/CORTE_MANGA no tienen almacén propio: van sin sugerencia.
ALMACEN_POR_DESTINO = {"APARTADO": "APARTADOS", "BORDADO": "BORDADOS"}


def programaciones_de(pedido, almacenes=None):
    """Parcialidades programadas del pedido. Con ``almacenes`` ({nombre en mayúsculas:
    Almacen}) agrega el almacén sugerido de cada destino."""
    filas = []
    for p in (pedido.programacion_conf or {}).get("programaciones") or []:
        fila = {"destino": p.get("destino"), "cantidad": p.get("cantidad"), "comentarios": p.get("comentarios") or ""}
        if almacenes is not None:
            almacen = almacenes.get(ALMACEN_POR_DESTINO.get(fila["destino"]))
            fila["almacen_sugerido"] = {"id": almacen.pk, "nombre": almacen.nombre} if almacen else None
        filas.append(fila)
    return filas


def almacenes_por_destino_de_sucursales(empresa_id, sucursal_ids):
    """{sucursal_id: {nombre en mayúsculas: Almacen}} de los almacenes de
    ``ALMACEN_POR_DESTINO`` en esas sucursales de la empresa. Una consulta.

    Solo almacenes ``ACTIVO`` con ``permite_entrada`` (lo que el POST acepta como
    destino). Si una sucursal tiene dos con el mismo nombre, gana el de menor pk.
    Una sucursal sin ninguno no aparece en el resultado.
    """
    if not sucursal_ids:
        return {}
    por_sucursal = {}
    almacenes = Almacen.objects.filter(
        empresa_id=empresa_id,
        sucursal_id__in=sucursal_ids,
        estatus="ACTIVO",
        permite_entrada=True,
        nombre__iregex=r"^(" + "|".join(ALMACEN_POR_DESTINO.values()) + r")$",
    ).order_by("pk")
    for almacen in almacenes:
        por_sucursal.setdefault(almacen.sucursal_id, {}).setdefault(almacen.nombre.upper(), almacen)
    return por_sucursal


def almacenes_por_destino(pedido):
    """{nombre: Almacen} de ``almacenes_por_destino_de_sucursales`` para la sucursal del pedido."""
    return almacenes_por_destino_de_sucursales(pedido.empresa_id, {pedido.sucursal_id}).get(pedido.sucursal_id, {})


def resolver_apartados_safe(empresa_id, sucursal_id):
    """Busca el almacén ``APARTADOS`` (iexact) sin fallar, sin filtrar ``estatus``
    ni flags.

    Devuelve el ``Almacen`` o ``None`` si no está configurado. Lo usa el POST
    (``sugerir_apartados_por_defecto``) cuando no llega ``almacen_destino``; las
    sugerencias del GET onboarding van por ``almacenes_por_destino``, que sí filtra.
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
    """Legacy name (deprecated). Usa ``sugerir_apartados_por_defecto``.

    Preservado para call sites que aún no migran; su semántica de "obligatorio
    con 400" ya no se aplica en el flujo POST onboarding (ahora el usuario
    selecciona cualquier almacén destino válido).
    """
    return sugerir_apartados_por_defecto(pedido)


def sugerir_apartados_por_defecto(pedido):
    """POST: sugiere el almacén ``APARTADOS`` (iexact) como destino default.

    A diferencia del nombre anterior NO lanza 400 si falta; devuelve el
    ``Almacen`` o ``None`` si no está configurado. El POST onboarding
    luego valida y — si falta tanto el body como la sugerencia — pide al
    usuario que seleccione un destino.
    """
    return resolver_apartados_safe(pedido.empresa_id, pedido.sucursal_id)


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
        "almacenes_origen": [],
        "almacenes_destino": [],
        "almacen_origen": None,
        "almacen_destino": None,
        "header": {
            "fecha_picking_sugerida": None,
            "folio_sugerido_preview": None,
            "tracker": {
                "pct_asignado_pedido": "0.0000",
                "pct_surtido_pedido": "0.0000",
                "total_prendas_pedido": "0",
                "total_asignado": "0",
                "total_surtido": "0",
            },
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
            # Solo se surte lo que mesa de control ya programó (``PATCH /pedidos/{id}/programar/``).
            programacion_conf__programaciones__0__isnull=False,
        )
        # ``empresa`` además de cliente/sucursal: ``armar_header_preview`` lee
        # ``pedido.empresa`` para el preview del folio, y sin esto era una
        # consulta suelta por petición.
        .select_related("cliente", "sucursal", "empresa")
        .order_by("-id")
    )
    if not es_staff:
        pedido_qs = pedido_qs.filter(sucursal_id__in=sucursal_ids)

    pedidos = list(pedido_qs[:50])
    # Almacén sugerido por destino: una consulta para las sucursales de los 50, no una por pedido.
    almacenes_por_sucursal = almacenes_por_destino_de_sucursales(
        empresa.pk, {pedido.sucursal_id for pedido in pedidos}
    )
    pedidos_payload = [
        {
            "id": pedido.id,
            "folio": pedido.folio,
            "cliente": pedido.cliente_id,
            "cliente_nombre": getattr(pedido.cliente, "nombre", None),
            "sucursal": pedido.sucursal_id,
            "sucursal_nombre": getattr(pedido.sucursal, "nombre", None),
            "programaciones": programaciones_de(pedido, almacenes_por_sucursal.get(pedido.sucursal_id, {})),
        }
        for pedido in pedidos
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
            "tipo_almacen": almacen.tipo_almacen,
            "permite_entrada": bool(getattr(almacen, "permite_entrada", False)),
            "permite_salida": bool(getattr(almacen, "permite_salida", False)),
            "permite_transferencia": bool(getattr(almacen, "permite_transferencia", False)),
        }
        for almacen in almacenes_qs[:100]
    ]
    # Subsets para selector UI (origen / destino) con fallback seguro:
    # si el catálogo no tiene bien prendidos los flags, caemos al catalogo
    # completo para no bloquear al operador.
    almacenes_origen_payload = [
        a for a in almacenes_payload if a["permite_salida"]
    ] or list(almacenes_payload)
    almacenes_destino_payload = [
        a for a in almacenes_payload if a["permite_entrada"]
    ] or list(almacenes_payload)

    return (
        pedido_qs,
        pedidos_payload,
        operadores_payload,
        almacenes_qs,
        almacenes_payload,
        almacenes_origen_payload,
        almacenes_destino_payload,
    )


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


def sugerir_almacenes(pedido, almacen_origen_actual, almacen_destino_actual, almacenes_destino=None):
    """Sugiere almacén origen (menor pk != destino) y destino = APARTADOS.

    Regla del GET onboarding: no exponer como sugerencia un ``origen`` igual
    al ``destino`` (APARTADOS), porque el POST rechaza ese caso con 400. Solo se
    sugieren almacenes ``ACTIVO`` con el flag que el POST exige: ``permite_salida``
    al origen y ``permite_entrada`` al destino. ``almacenes_destino`` es el
    resultado de ``almacenes_por_destino(pedido)`` si el llamador ya lo tiene.
    """
    origen_sugerido = almacen_origen_actual
    destino_sugerido = almacen_destino_actual

    if destino_sugerido is None:
        if almacenes_destino is None:
            almacenes_destino = almacenes_por_destino(pedido)
        destino_sugerido = almacenes_destino.get(ALMACEN_POR_DESTINO["APARTADO"])

    if origen_sugerido is None:
        candidatos = Almacen.objects.filter(
            empresa_id=pedido.empresa_id,
            sucursal_id=pedido.sucursal_id,
            estatus="ACTIVO",
            permite_salida=True,
        ).order_by("pk")
        exclude_pks = []
        if destino_sugerido is not None:
            exclude_pks.append(destino_sugerido.pk)
        origen_sugerido = candidatos.exclude(pk__in=exclude_pks).first()
        if origen_sugerido is None and destino_sugerido is not None:
            origen_sugerido = candidatos.first()

    return origen_sugerido, destino_sugerido


def armar_header_preview(pedido):
    """Encabezado del onboarding: fecha sugerida + preview del folio picking."""
    fecha_picking_sugerida = timezone.now()
    folio_sugerido_preview = folio_preview(pedido.empresa, pedido.sucursal)
    return {
        "fecha_picking_sugerida": fecha_picking_sugerida.isoformat(),
        "folio_sugerido_preview": folio_sugerido_preview,
    }
