"""Cálculo de los reportes por periodo de inventario (EC-814).

``reporte_existencias`` (``existencias/reporte-existencias-periodo/``) y
``reporte_movimientos`` (``movimientos/reporte-movimientos-periodo/``) resuelven
el alcance del usuario, validan y aplican los filtros y calculan el reporte
COMPLETO, sin paginar. Los usan el endpoint JSON (que pagina el resultado) y la
exportación a PDF/XLSX, así que ambos ven exactamente las mismas filas y
responden los mismos 400.

Alcance: almacenes de las empresas del usuario (la activa + el M2M
``empresas``) Y de sus ``sucursales_permitidas()``; los query params solo lo
acotan. El superusuario ve todo.
"""
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal

from django.db import models
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError

from auditoria.models import AuditoriaEvento
from catalogo.models import Producto, ProductoVariante
from inventarios.models import Almacen, Existencia, MovimientoInventarioDetalle

TIPOS_MOVIMIENTO = ("ENTRADA", "SALIDA", "AJUSTE", "TRANSFERENCIA")


def to_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def to_decimal(value):
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def quantize_qty(value):
    return to_decimal(value).quantize(Decimal("0.0001"))


def quantize_money(value):
    return to_decimal(value).quantize(Decimal("0.01"))


def _fecha(query_params, nombre):
    """Fecha obligatoria ``YYYY-MM-DD``; 400 con la clave ``nombre``."""
    try:
        fecha = parse_date((query_params.get(nombre) or "").strip())
    except ValueError:
        # ``parse_date`` lanza ``ValueError`` con una fecha bien formada que no
        # existe (2026-02-30): era un 500 (EC-822). El formato sí es el pedido,
        # así que el mensaje no es el de "requerida".
        raise ValidationError({nombre: f"{nombre} no es una fecha válida: ese día no existe."})
    if not fecha:
        raise ValidationError({nombre: f"{nombre} es requerida con formato YYYY-MM-DD."})
    return fecha


def parse_fechas(query_params):
    """``fecha_inicio``/``fecha_final`` obligatorias (YYYY-MM-DD), en hora local."""
    fecha_inicio = _fecha(query_params, "fecha_inicio")
    fecha_final = _fecha(query_params, "fecha_final")

    if fecha_final < fecha_inicio:
        raise ValidationError({"fecha_final": "fecha_final no puede ser menor a fecha_inicio."})

    tz = timezone.get_current_timezone()
    inicio_dt = timezone.make_aware(datetime.combine(fecha_inicio, time.min), tz)
    final_dt = timezone.make_aware(datetime.combine(fecha_final, time.max), tz)
    return fecha_inicio, fecha_final, inicio_dt, final_dt


def id_positivo(valor, clave):
    """Id de un query param: vacío → ``None``; no numérico, 0 o negativo → 400
    con ``clave``. Ignorarlo quitaba el filtro en silencio (#805, #815, EC-822)."""
    raw = (valor or "").strip()
    if not raw:
        return None
    try:
        numero = int(raw)
    except ValueError:
        numero = 0
    if numero <= 0:
        raise ValidationError({clave: "Debe ser un id numérico mayor a 0."})
    return numero


def almacen_del_query(query_params):
    """``almacen``/``almacen_id`` de los reportes por periodo (EC-822): el primer
    alias NO vacío, estricto, y el 400 siempre con la clave ``almacen``. Antes,
    ``almacen=abc&almacen_id=5`` tomaba ``abc`` y se quedaba sin filtro."""
    raw = next((v for v in (query_params.get("almacen"), query_params.get("almacen_id")) if (v or "").strip()), None)
    return id_positivo(raw, "almacen")


def almacen_requerido(query_params):
    """``almacen``/``almacen_id`` obligatorio (exportación de existencias)."""
    almacen_id = almacen_del_query(query_params)
    if almacen_id is None:
        raise ValidationError({"almacen": "almacen es requerido para exportar el reporte de existencias."})
    return almacen_id


def almacenes_en_alcance(user, qs=None):
    """Almacenes sobre los que opera un usuario NO superusuario (``qs`` acotado,
    o todos los almacenes).

    Única definición del alcance de almacenes: la usan existencias, las
    operaciones de inventario y los reportes por periodo. Empresas del usuario
    (solo la activa, EC-816) Y sus sucursales. Sin empresa activa o sin
    sucursales el alcance queda vacío. El superusuario se trata aparte.
    """
    from nucleo.scope import empresa_ids_lectura

    empresa_ids = empresa_ids_lectura(user)
    sucursal_ids = list(user.sucursales_permitidas())
    qs = Almacen.objects.all() if qs is None else qs
    return qs.filter(models.Q(empresa_id__in=empresa_ids) & models.Q(sucursal_id__in=sucursal_ids))


def _almacenes_en_alcance(user, qs):
    return qs if user.is_superuser else almacenes_en_alcance(user, qs)


def almacenes_del_reporte(user, query_params, almacen_id):
    """Almacenes del reporte de existencias (y de
    ``reporte-existencias-produccion-compras``): alcance del usuario acotado por
    ``empresa``, ``sucursal`` (y sus alias ``*_id``) y por ``almacen_id``, que el
    llamador ya leyó con ``almacen_del_query`` (una vez por petición)."""
    qs = _almacenes_en_alcance(user, Almacen.objects.select_related("empresa", "sucursal").all())

    empresa_id = to_int(query_params.get("empresa") or query_params.get("empresa_id"))
    sucursal_id = to_int(query_params.get("sucursal") or query_params.get("sucursal_id"))

    if empresa_id:
        qs = qs.filter(empresa_id=empresa_id)
    if sucursal_id:
        qs = qs.filter(sucursal_id=sucursal_id)
    if almacen_id:
        qs = qs.filter(pk=almacen_id)
    return qs


def almacen_ids_movimientos(user, query_params):
    """Ids de almacén del reporte de movimientos (solo acota ``almacen``) y el
    ``almacen`` pedido (estricto, ``almacen_del_query``). El auxiliar también
    lo usa, pero valida ``almacen`` antes con la misma regla."""
    almacen_id = almacen_del_query(query_params)
    qs = _almacenes_en_alcance(user, Almacen.objects.all())
    if almacen_id:
        qs = qs.filter(pk=almacen_id)
    return set(qs.values_list("pk", flat=True)), almacen_id


# --- Existencias por periodo ---------------------------------------------------


def _iter_auditoria_items(eventos, allowed_almacen_ids, producto_id=None, producto_variante_id=None):
    for evento in eventos.iterator():
        payload = evento.despues_json or evento.antes_json or {}
        evento_almacen_id = to_int(payload.get("almacen_id"))
        for item in payload.get("items", []) or []:
            # Most audit writers (inventarios manual, compras) store almacen_id
            # only at the event level, not per item; fall back to it so those
            # movements aren't silently dropped from the period aggregation.
            almacen_id = to_int(item.get("almacen_id")) or evento_almacen_id
            if not almacen_id or almacen_id not in allowed_almacen_ids:
                continue

            item_producto_id = to_int(item.get("producto_id"))
            item_variante_id = to_int(item.get("producto_variante_id"))

            if producto_variante_id and item_variante_id != producto_variante_id:
                continue
            if producto_id and item_producto_id != producto_id:
                continue

            yield {
                "almacen_id": almacen_id,
                "producto_id": item_producto_id,
                "producto_variante_id": item_variante_id,
                "delta": to_decimal(item.get("delta")),
            }


# Almacén de origen/destino de un renglón de movimiento para el costo: el de su
# ubicación y, si no tiene, el del documento. Se toma el primero no nulo.
_CAMPOS_ALMACEN_ORIGEN = (
    "ubicacion_origen__almacen_id",
    "movimiento_inventario__transferencia__almacen_origen_id",
    "movimiento_inventario__ajuste_inventario__almacen_id",
)
_CAMPOS_ALMACEN_DESTINO = (
    "ubicacion_destino__almacen_id",
    "movimiento_inventario__recepcion__almacen_id",
    "movimiento_inventario__transferencia__almacen_destino_id",
    "movimiento_inventario__ajuste_inventario__almacen_id",
)


def _primero(valores):
    for valor in valores:
        if valor:
            return valor
    return None


def _totales_existencias(totals):
    return {
        "existencia_inicial": str(quantize_qty(totals["existencia_inicial"])),
        "entradas": str(quantize_qty(totals["entradas"])),
        "salidas": str(quantize_qty(totals["salidas"])),
        "existencia_final": str(quantize_qty(totals["existencia_final"])),
        "costo_total_existencia_final": str(quantize_money(totals["costo_total_existencia_final"])),
    }


def _totales_en_cero():
    return {
        "existencia_inicial": Decimal("0"),
        "entradas": Decimal("0"),
        "salidas": Decimal("0"),
        "existencia_final": Decimal("0"),
        "costo_total_existencia_final": Decimal("0"),
    }


_SIN_LEER = object()


def reporte_existencias(user, query_params, almacen_id=_SIN_LEER):
    """Reporte de existencias del periodo, completo y sin paginar.

    Regresa ``fecha_inicio``/``fecha_final`` (``date``), los filtros
    ``almacen_id``/``empresa_id``/``sucursal_id``/``producto_id``/
    ``producto_variante_id``, ``almacenes``
    (``{id_almacen: {codigo, nombre, ...}}`` de los almacenes en alcance),
    ``sin_almacenes`` (ningún almacén en alcance), ``results``, ``resumen`` y
    ``resumen_por_almacen`` con el mismo formato que la respuesta JSON.
    """
    fecha_inicio, fecha_final, inicio_dt, final_dt = parse_fechas(query_params)
    qp = query_params
    # La exportación ya lo leyó con ``almacen_requerido``; el JSON lo lee aquí,
    # después de las fechas (mismo orden de los 400).
    if almacen_id is _SIN_LEER:
        almacen_id = almacen_del_query(qp)
    producto_id = to_int(qp.get("producto") or qp.get("producto_id"))
    producto_variante_id = to_int(qp.get("producto_variante") or qp.get("producto_variante_id"))

    almacenes = list(
        almacenes_del_reporte(user, qp, almacen_id).values(
            "id_almacen",
            "codigo",
            "nombre",
            "empresa_id",
            "sucursal_id",
        )
    )
    almacen_map = {row["id_almacen"]: row for row in almacenes}
    allowed_almacen_ids = set(almacen_map)

    reporte = {
        "fecha_inicio": fecha_inicio,
        "fecha_final": fecha_final,
        "almacen_id": almacen_id,
        "empresa_id": to_int(qp.get("empresa") or qp.get("empresa_id")),
        "sucursal_id": to_int(qp.get("sucursal") or qp.get("sucursal_id")),
        "producto_id": producto_id,
        "producto_variante_id": producto_variante_id,
        "almacenes": almacen_map,
        "sin_almacenes": not allowed_almacen_ids,
        "results": [],
        "resumen": _totales_existencias(_totales_en_cero()),
        "resumen_por_almacen": [],
    }
    if not allowed_almacen_ids:
        return reporte

    empresa_ids = {row["empresa_id"] for row in almacenes if row["empresa_id"]}

    current_map = defaultdict(lambda: Decimal("0"))
    keys = set()

    # Only the identity keys and cantidad are consumed here, so fetch scalar
    # tuples via values_list instead of materializing full Existencia + related
    # model graphs for thousands of rows (producto/variante display detail is
    # resolved later via productos_map/variantes_map).
    current_qs = Existencia.objects.filter(almacen_id__in=allowed_almacen_ids)

    if producto_variante_id:
        current_qs = current_qs.filter(producto_variante_id=producto_variante_id)
    if producto_id:
        current_qs = current_qs.filter(
            models.Q(producto_id=producto_id)
            | models.Q(producto_variante__producto_id=producto_id)
        )

    current_rows = current_qs.values_list(
        "almacen_id",
        "producto_id",
        "producto_variante_id",
        "producto_variante__producto_id",
        "cantidad",
    )
    for (
        row_almacen_id,
        row_producto_id,
        row_variante_id,
        row_variante_producto_id,
        row_cantidad,
    ) in current_rows.iterator():
        key = (
            row_almacen_id,
            row_producto_id or row_variante_producto_id,
            row_variante_id,
        )
        current_map[key] += to_decimal(row_cantidad)
        keys.add(key)

    auditoria_base = AuditoriaEvento.objects.filter(
        modulo="inventarios",
        tabla="existencias",
        empresa_id__in=empresa_ids,
    ).order_by("created_at", "id_evento")

    period_delta_map = defaultdict(lambda: Decimal("0"))
    period_in_map = defaultdict(lambda: Decimal("0"))
    period_out_map = defaultdict(lambda: Decimal("0"))
    post_end_delta_map = defaultdict(lambda: Decimal("0"))

    period_events = auditoria_base.filter(created_at__gte=inicio_dt, created_at__lte=final_dt)
    for item in _iter_auditoria_items(
        period_events,
        allowed_almacen_ids=allowed_almacen_ids,
        producto_id=producto_id,
        producto_variante_id=producto_variante_id,
    ):
        key = (item["almacen_id"], item["producto_id"], item["producto_variante_id"])
        delta = item["delta"]
        period_delta_map[key] += delta
        if delta >= 0:
            period_in_map[key] += delta
        else:
            period_out_map[key] += abs(delta)
        keys.add(key)

    post_end_events = auditoria_base.filter(created_at__gt=final_dt)
    for item in _iter_auditoria_items(
        post_end_events,
        allowed_almacen_ids=allowed_almacen_ids,
        producto_id=producto_id,
        producto_variante_id=producto_variante_id,
    ):
        key = (item["almacen_id"], item["producto_id"], item["producto_variante_id"])
        post_end_delta_map[key] += item["delta"]
        keys.add(key)

    product_ids = {key[1] for key in keys if key[1]}
    variante_ids = {key[2] for key in keys if key[2]}

    productos_map = {
        producto.pk: producto
        for producto in Producto.objects.filter(pk__in=product_ids).only(
            "id", "nombre", "precio_base"
        )
    }
    variantes_map = {
        variante.pk: variante
        for variante in ProductoVariante.objects.select_related("producto", "color", "talla")
        .filter(pk__in=variante_ids)
    }

    cost_map = {}
    if keys:
        movement_cost_qs = (
            MovimientoInventarioDetalle.objects.filter(
                movimiento_inventario__fecha_movimiento__lte=final_dt,
                costo_unitario__gt=0,
            )
            .filter(
                models.Q(ubicacion_origen__almacen_id__in=allowed_almacen_ids)
                | models.Q(ubicacion_destino__almacen_id__in=allowed_almacen_ids)
                | models.Q(movimiento_inventario__recepcion__almacen_id__in=allowed_almacen_ids)
                | models.Q(movimiento_inventario__ajuste_inventario__almacen_id__in=allowed_almacen_ids)
                | models.Q(movimiento_inventario__transferencia__almacen_origen_id__in=allowed_almacen_ids)
                | models.Q(movimiento_inventario__transferencia__almacen_destino_id__in=allowed_almacen_ids)
            )
            .order_by("-movimiento_inventario__fecha_movimiento", "-id")
        )
        if producto_variante_id:
            movement_cost_qs = movement_cost_qs.filter(producto_variante_id=producto_variante_id)
        if producto_id:
            movement_cost_qs = movement_cost_qs.filter(producto_id=producto_id)

        # Del más reciente al más antiguo: el primer costo de cada llave gana.
        # Solo bajan escalares (sin instanciar modelos) y por bloques, porque
        # una llave sin movimiento costeado obliga a recorrer todo el historial.
        filas_costo = movement_cost_qs.values_list(
            "producto_id",
            "producto_variante_id",
            "costo_unitario",
            *_CAMPOS_ALMACEN_ORIGEN,
            *_CAMPOS_ALMACEN_DESTINO,
        )
        n_origen = len(_CAMPOS_ALMACEN_ORIGEN)
        for producto_key_id, variante_key_id, costo, *almacenes_fila in filas_costo.iterator(chunk_size=2000):
            origen_id = _primero(almacenes_fila[:n_origen])
            destino_id = _primero(almacenes_fila[n_origen:])
            for costo_almacen_id in {origen_id, destino_id}:
                key = (costo_almacen_id, producto_key_id, variante_key_id)
                if costo_almacen_id and key in keys and key not in cost_map:
                    cost_map[key] = to_decimal(costo)
            if len(cost_map) >= len(keys):
                break

    detalle_rows = []
    resumen_por_almacen = defaultdict(_totales_en_cero)
    resumen_total = _totales_en_cero()

    for key in sorted(keys, key=lambda row: (row[0], row[1] or 0, row[2] or 0)):
        row_almacen_id, producto_key_id, variante_key_id = key
        current_qty = current_map[key]
        period_delta = period_delta_map[key]
        entradas = period_in_map[key]
        salidas = period_out_map[key]
        existencia_final = current_qty - post_end_delta_map[key]
        existencia_inicial = existencia_final - period_delta
        costo_unitario = cost_map.get(key, Decimal("0"))
        costo_existencia_final = quantize_money(existencia_final * costo_unitario)

        variante = variantes_map.get(variante_key_id)
        producto = variantes_map[variante_key_id].producto if variante else productos_map.get(producto_key_id)
        almacen = almacen_map.get(row_almacen_id, {})

        detalle_rows.append(
            {
                "almacen_id": row_almacen_id,
                "almacen_codigo": almacen.get("codigo"),
                "almacen_nombre": almacen.get("nombre"),
                "producto_id": producto_key_id,
                "producto_variante_id": variante_key_id,
                "producto_nombre": getattr(producto, "nombre", None),
                "sku": getattr(variante, "sku", None) if variante else None,
                "color": getattr(getattr(variante, "color", None), "nombre", None) if variante else None,
                "talla": getattr(getattr(variante, "talla", None), "nombre", None) if variante else None,
                "existencia_inicial": str(quantize_qty(existencia_inicial)),
                "entradas": str(quantize_qty(entradas)),
                "salidas": str(quantize_qty(salidas)),
                "existencia_final": str(quantize_qty(existencia_final)),
                "costo_unitario_final": str(quantize_money(costo_unitario)),
                "costo_existencia_final": str(costo_existencia_final),
            }
        )

        for totals in (resumen_por_almacen[row_almacen_id], resumen_total):
            totals["existencia_inicial"] += existencia_inicial
            totals["entradas"] += entradas
            totals["salidas"] += salidas
            totals["existencia_final"] += existencia_final
            totals["costo_total_existencia_final"] += to_decimal(costo_existencia_final)

    resumen_almacenes_payload = []
    for resumen_almacen_id, totals in sorted(resumen_por_almacen.items(), key=lambda item: item[0]):
        almacen = almacen_map.get(resumen_almacen_id, {})
        resumen_almacenes_payload.append(
            {
                "almacen_id": resumen_almacen_id,
                "almacen_codigo": almacen.get("codigo"),
                "almacen_nombre": almacen.get("nombre"),
                **_totales_existencias(totals),
            }
        )

    reporte["results"] = detalle_rows
    reporte["resumen"] = _totales_existencias(resumen_total)
    reporte["resumen_por_almacen"] = resumen_almacenes_payload
    return reporte


# --- Movimientos por periodo ---------------------------------------------------


def tipo_movimiento_requerido(query_params):
    tipo_movimiento = (query_params.get("tipo_movimiento") or "").strip().upper()
    if tipo_movimiento not in TIPOS_MOVIMIENTO:
        raise ValidationError(
            {
                "tipo_movimiento": (
                    "tipo_movimiento es requerido y debe ser ENTRADA, SALIDA, AJUSTE o TRANSFERENCIA."
                )
            }
        )
    return tipo_movimiento


# Almacén de cada lado (origen/destino) de un renglón de movimiento, por tipo y
# en orden de precedencia: la ubicación del renglón, el ``almacen`` del detalle
# (existencia sin ubicación, #803) y el del documento. La transferencia usa su
# almacén de origen/destino, no ``detalle.almacen``, que no dice de qué lado es
# (#801). Lo usan el reporte de movimientos (filtra y muestra con el mismo
# almacén, EC-820) y el detalle de un movimiento formal.
_RUTAS_AJUSTE = ("movimiento_inventario__ajuste_inventario__almacen",)
RUTAS_ALMACEN_POR_LADO = {
    "ENTRADA": {
        "destino": (
            "ubicacion_destino__almacen",
            "almacen",
            "movimiento_inventario__recepcion__almacen",
            "movimiento_inventario__ajuste_inventario__almacen",
        ),
    },
    "SALIDA": {
        "origen": (
            "ubicacion_origen__almacen",
            "almacen",
            "movimiento_inventario__ajuste_inventario__almacen",
        ),
    },
    # El ajuste afecta un solo almacén: el mismo de los dos lados.
    "AJUSTE": {"origen": _RUTAS_AJUSTE, "destino": _RUTAS_AJUSTE},
    "TRANSFERENCIA": {
        "origen": ("ubicacion_origen__almacen", "movimiento_inventario__transferencia__almacen_origen"),
        "destino": ("ubicacion_destino__almacen", "movimiento_inventario__transferencia__almacen_destino"),
    },
}


def _primer_almacen(rutas, campo):
    """``campo`` del primer almacén no nulo de ``rutas``, en SQL."""
    return models.Case(
        *(models.When(**{f"{ruta}__isnull": False}, then=models.F(f"{ruta}__{campo}")) for ruta in rutas),
        default=None,
    )


def anotar_almacenes(qs, tipo_movimiento, lados=None, campos=("codigo", "nombre")):
    """Anota en un queryset de ``MovimientoInventarioDetalle`` el almacén de
    cada lado del tipo (o solo de ``lados``): ``almacen_{lado}_res`` (id) y
    ``almacen_{lado}_res_{campo}``.

    Todo sale de ``RUTAS_ALMACEN_POR_LADO``, en la misma consulta: no hace
    falta ``select_related`` ni recorrer relaciones por fila.
    """
    anotaciones = {}
    for lado, rutas in RUTAS_ALMACEN_POR_LADO[tipo_movimiento].items():
        if lados is not None and lado not in lados:
            continue
        ids = [f"{ruta}_id" for ruta in rutas]
        anotaciones[f"almacen_{lado}_res"] = Coalesce(*ids) if len(ids) > 1 else models.F(ids[0])
        for campo in campos:
            anotaciones[f"almacen_{lado}_res_{campo}"] = _primer_almacen(rutas, campo)
    return qs.annotate(**anotaciones)


def reporte_movimientos(user, query_params):
    """Reporte de movimientos de un tipo en el periodo, completo y sin paginar.

    Regresa ``tipo_movimiento``, ``fecha_inicio``/``fecha_final`` (``date``),
    ``almacen_id`` (el filtro pedido), ``sin_almacenes``, ``results`` y
    ``resumen`` con el mismo formato que la respuesta JSON.
    """
    tipo_movimiento = tipo_movimiento_requerido(query_params)
    fecha_inicio, fecha_final, inicio_dt, final_dt = parse_fechas(query_params)
    allowed_almacen_ids, almacen_id = almacen_ids_movimientos(user, query_params)

    reporte = {
        "tipo_movimiento": tipo_movimiento,
        "fecha_inicio": fecha_inicio,
        "fecha_final": fecha_final,
        "almacen_id": almacen_id,
        "sin_almacenes": not allowed_almacen_ids,
        "results": [],
        "resumen": {
            "total_movimientos": 0,
            "total_registros": 0,
            "total_cantidad": "0.0000",
        },
    }
    if not allowed_almacen_ids:
        return reporte

    detalles_qs = (
        MovimientoInventarioDetalle.objects.select_related(
            "movimiento_inventario",
            "movimiento_inventario__usuario",
            "movimiento_inventario__pedido",
            # Observaciones/motivo del ajuste y folio de la transferencia. El
            # almacén de la fila sale de ``anotar_almacenes``.
            "movimiento_inventario__ajuste_inventario",
            "movimiento_inventario__op",
            "movimiento_inventario__transferencia",
            "producto",
            "producto_variante",
            "producto_variante__producto",
            "producto_variante__color",
            "producto_variante__talla",
            # ``str(ubicacion)`` lleva el nombre de su almacén.
            "ubicacion_origen__almacen",
            "ubicacion_destino__almacen",
        )
        .filter(
            movimiento_inventario__activo=True,
            movimiento_inventario__tipo_movimiento=tipo_movimiento,
            movimiento_inventario__fecha_movimiento__gte=inicio_dt,
            movimiento_inventario__fecha_movimiento__lte=final_dt,
        )
        .order_by("-movimiento_inventario__fecha_movimiento", "-id")
    )

    if not user.is_superuser:
        # Defensa: además del almacén, el movimiento debe ser de la empresa
        # activa del usuario (como el auxiliar).
        from nucleo.scope import empresa_ids_lectura

        detalles_qs = detalles_qs.filter(movimiento_inventario__empresa_id__in=empresa_ids_lectura(user))

    # Un renglón entra si alguno de sus lados está en alcance, y se muestra con
    # el primero que lo esté (origen antes que destino): en la transferencia,
    # el origen si está en alcance, si no el destino. Una fila por renglón.
    lados = tuple(RUTAS_ALMACEN_POR_LADO[tipo_movimiento])
    if tipo_movimiento == "AJUSTE":
        lados = ("origen",)  # los dos lados son el mismo almacén
    filtro = models.Q()
    for lado in lados:
        filtro |= models.Q(**{f"almacen_{lado}_res__in": allowed_almacen_ids})
    detalles_qs = anotar_almacenes(detalles_qs, tipo_movimiento, lados).filter(filtro)

    resultados = []
    total_cantidad = Decimal("0")
    movimiento_ids = set()

    for detalle in detalles_qs:
        movimiento = detalle.movimiento_inventario
        variante = detalle.producto_variante
        producto = getattr(variante, "producto", None) if variante else detalle.producto

        # El filtro garantiza que alguno de los lados está en alcance.
        lado = next(c for c in lados if getattr(detalle, f"almacen_{c}_res") in allowed_almacen_ids)
        almacen_fila = {
            "almacen_id": getattr(detalle, f"almacen_{lado}_res"),
            "almacen_codigo": getattr(detalle, f"almacen_{lado}_res_codigo"),
            "almacen_nombre": getattr(detalle, f"almacen_{lado}_res_nombre"),
        }
        if tipo_movimiento == "AJUSTE":
            ubicacion = detalle.ubicacion_destino or detalle.ubicacion_origen
        else:
            ubicacion = detalle.ubicacion_origen if lado == "origen" else detalle.ubicacion_destino

        usuario = getattr(movimiento, "usuario", None)
        pedido = getattr(movimiento, "pedido", None)
        transferencia = getattr(movimiento, "transferencia", None)
        cantidad = to_decimal(detalle.cantidad)
        costo_unitario = to_decimal(detalle.costo_unitario)
        total_cantidad += cantidad
        movimiento_ids.add(movimiento.pk)

        resultados.append(
            {
                "movimiento_inventario_id": movimiento.pk,
                "movimiento_detalle_id": detalle.pk,
                "tipo_movimiento": movimiento.tipo_movimiento,
                "fecha_movimiento": movimiento.fecha_movimiento,
                **almacen_fila,
                "ubicacion_id": getattr(ubicacion, "pk", None),
                "ubicacion_nombre": str(ubicacion) if ubicacion else None,
                "producto_id": getattr(producto, "pk", None),
                "producto_variante_id": getattr(variante, "pk", None),
                "sku": getattr(variante, "sku", None),
                "producto_nombre": (
                    getattr(variante, "nombre", None)
                    or getattr(producto, "nombre", None)
                ),
                "producto_base_nombre": getattr(producto, "nombre", None),
                "color": getattr(getattr(variante, "color", None), "nombre", None)
                if variante
                else None,
                "talla": getattr(getattr(variante, "talla", None), "nombre", None)
                if variante
                else None,
                "cantidad": str(cantidad.quantize(Decimal("0.0001"))),
                "costo_unitario": str(costo_unitario.quantize(Decimal("0.01"))),
                "costo_total": str((cantidad * costo_unitario).quantize(Decimal("0.01"))),
                "pedido_id": getattr(pedido, "pk", None),
                "pedido_folio": getattr(pedido, "folio", None),
                "transferencia_id": getattr(transferencia, "pk", None),
                "transferencia_folio": getattr(transferencia, "folio", None),
                "recepcion_id": getattr(movimiento, "recepcion_id", None),
                "ajuste_inventario_id": getattr(movimiento, "ajuste_inventario_id", None),
                "op_id": getattr(movimiento, "op_id", None),
                "usuario_id": getattr(usuario, "pk", None),
                "usuario_nombre": (
                    usuario.get_full_name().strip() or usuario.email
                    if usuario
                    else None
                ),
                "observaciones": movimiento.observaciones,
                "comentarios": (
                    getattr(getattr(movimiento, "ajuste_inventario", None), "observaciones", None)
                    or movimiento.observaciones
                ),
                "motivo_ajuste": getattr(
                    getattr(movimiento, "ajuste_inventario", None), "motivo", None
                ),
            }
        )

    reporte["results"] = resultados
    reporte["resumen"] = {
        "total_movimientos": len(movimiento_ids),
        "total_registros": len(resultados),
        "total_cantidad": str(total_cantidad.quantize(Decimal("0.0001"))),
    }
    return reporte
