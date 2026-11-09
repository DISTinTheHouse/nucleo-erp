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


def parse_fechas(query_params):
    """``fecha_inicio``/``fecha_final`` obligatorias (YYYY-MM-DD), en hora local."""
    fecha_inicio_raw = (query_params.get("fecha_inicio") or "").strip()
    fecha_final_raw = (query_params.get("fecha_final") or "").strip()
    fecha_inicio = parse_date(fecha_inicio_raw)
    fecha_final = parse_date(fecha_final_raw)

    if not fecha_inicio:
        raise ValidationError({"fecha_inicio": "fecha_inicio es requerida con formato YYYY-MM-DD."})
    if not fecha_final:
        raise ValidationError({"fecha_final": "fecha_final es requerida con formato YYYY-MM-DD."})
    if fecha_final < fecha_inicio:
        raise ValidationError({"fecha_final": "fecha_final no puede ser menor a fecha_inicio."})

    tz = timezone.get_current_timezone()
    inicio_dt = timezone.make_aware(datetime.combine(fecha_inicio, time.min), tz)
    final_dt = timezone.make_aware(datetime.combine(fecha_final, time.max), tz)
    return fecha_inicio, fecha_final, inicio_dt, final_dt


def almacen_requerido(query_params):
    """``almacen``/``almacen_id`` obligatorio (exportación de existencias)."""
    almacen_id = to_int(query_params.get("almacen") or query_params.get("almacen_id"))
    if not almacen_id:
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


def almacenes_del_reporte(user, query_params):
    """Almacenes del reporte de existencias: alcance del usuario acotado por
    ``empresa``, ``sucursal`` y ``almacen`` (y sus alias ``*_id``)."""
    qs = _almacenes_en_alcance(user, Almacen.objects.select_related("empresa", "sucursal").all())

    empresa_id = to_int(query_params.get("empresa") or query_params.get("empresa_id"))
    sucursal_id = to_int(query_params.get("sucursal") or query_params.get("sucursal_id"))
    almacen_id = to_int(query_params.get("almacen") or query_params.get("almacen_id"))

    if empresa_id:
        qs = qs.filter(empresa_id=empresa_id)
    if sucursal_id:
        qs = qs.filter(sucursal_id=sucursal_id)
    if almacen_id:
        qs = qs.filter(pk=almacen_id)
    return qs


def almacen_ids_movimientos(user, query_params):
    """Ids de almacén del reporte de movimientos (solo acota ``almacen``) y el
    ``almacen`` pedido."""
    almacen_id = to_int(query_params.get("almacen") or query_params.get("almacen_id"))
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


def reporte_existencias(user, query_params):
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
    almacen_id = to_int(qp.get("almacen") or qp.get("almacen_id"))
    producto_id = to_int(qp.get("producto") or qp.get("producto_id"))
    producto_variante_id = to_int(qp.get("producto_variante") or qp.get("producto_variante_id"))

    almacenes = list(
        almacenes_del_reporte(user, qp).values(
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


# Almacén de cada renglón del reporte de movimientos, en orden de precedencia:
# la ubicación del renglón, el ``almacen`` del detalle (existencia sin
# ubicación, #803) y el del documento. El mismo orden filtra (``Coalesce``) y
# se muestra, así que el almacén que pasó el alcance es el que sale en la fila
# (EC-820). La transferencia usa su almacén de origen/destino, no
# ``detalle.almacen``, que no dice de qué lado es (#801).
_RUTAS_ALMACEN_MOVIMIENTO = {
    "ENTRADA": (
        "ubicacion_destino__almacen",
        "almacen",
        "movimiento_inventario__recepcion__almacen",
        "movimiento_inventario__ajuste_inventario__almacen",
    ),
    "SALIDA": (
        "ubicacion_origen__almacen",
        "almacen",
        "movimiento_inventario__ajuste_inventario__almacen",
    ),
    "AJUSTE": ("movimiento_inventario__ajuste_inventario__almacen",),
}
_RUTAS_ALMACEN_TRANSFERENCIA_ORIGEN = (
    "ubicacion_origen__almacen",
    "movimiento_inventario__transferencia__almacen_origen",
)
_RUTAS_ALMACEN_TRANSFERENCIA_DESTINO = (
    "ubicacion_destino__almacen",
    "movimiento_inventario__transferencia__almacen_destino",
)


def _almacen_coalesce(rutas):
    """Id del primer almacén no nulo de ``rutas`` (en SQL)."""
    campos = [f"{ruta}_id" for ruta in rutas]
    return Coalesce(*campos) if len(campos) > 1 else models.F(campos[0])


def _almacen_de(detalle, rutas, almacen_id):
    """El objeto ``Almacen`` (ya cargado por ``select_related``) cuyo id resolvió
    el ``Coalesce``."""
    if not almacen_id:
        return None
    for ruta in rutas:
        obj = detalle
        for parte in ruta.split("__"):
            obj = getattr(obj, parte, None)
            if obj is None:
                break
        if obj is not None and obj.pk == almacen_id:
            return obj
    return None


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
            # El almacén del documento se lee cuando el renglón no trae
            # ubicación (ENTRADA/SALIDA/AJUSTE): sin el join era una consulta
            # por renglón.
            "movimiento_inventario__recepcion__almacen",
            "movimiento_inventario__ajuste_inventario__almacen",
            "movimiento_inventario__op",
            "movimiento_inventario__transferencia",
            "movimiento_inventario__transferencia__almacen_origen",
            "movimiento_inventario__transferencia__almacen_destino",
            "almacen",
            "producto",
            "producto_variante",
            "producto_variante__producto",
            "producto_variante__color",
            "producto_variante__talla",
            "ubicacion_origen",
            "ubicacion_origen__almacen",
            "ubicacion_destino",
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

    if tipo_movimiento == "TRANSFERENCIA":
        detalles_qs = detalles_qs.annotate(
            almacen_origen_resuelto=_almacen_coalesce(_RUTAS_ALMACEN_TRANSFERENCIA_ORIGEN),
            almacen_destino_resuelto=_almacen_coalesce(_RUTAS_ALMACEN_TRANSFERENCIA_DESTINO),
        ).filter(
            models.Q(almacen_origen_resuelto__in=allowed_almacen_ids)
            | models.Q(almacen_destino_resuelto__in=allowed_almacen_ids)
        )
    else:
        rutas_almacen = _RUTAS_ALMACEN_MOVIMIENTO[tipo_movimiento]
        detalles_qs = detalles_qs.annotate(
            almacen_resuelto=_almacen_coalesce(rutas_almacen),
        ).filter(almacen_resuelto__in=allowed_almacen_ids)

    resultados = []
    total_cantidad = Decimal("0")
    movimiento_ids = set()

    for detalle in detalles_qs:
        movimiento = detalle.movimiento_inventario
        variante = detalle.producto_variante
        producto = getattr(variante, "producto", None) if variante else detalle.producto

        if tipo_movimiento == "TRANSFERENCIA":
            # Una fila por renglón: el origen si está en alcance, si no el
            # destino (el filtro garantiza que uno de los dos lo está).
            if detalle.almacen_origen_resuelto in allowed_almacen_ids:
                almacen = _almacen_de(detalle, _RUTAS_ALMACEN_TRANSFERENCIA_ORIGEN, detalle.almacen_origen_resuelto)
                ubicacion = detalle.ubicacion_origen
            else:
                almacen = _almacen_de(detalle, _RUTAS_ALMACEN_TRANSFERENCIA_DESTINO, detalle.almacen_destino_resuelto)
                ubicacion = detalle.ubicacion_destino
        else:
            almacen = _almacen_de(detalle, rutas_almacen, detalle.almacen_resuelto)
            if tipo_movimiento == "ENTRADA":
                ubicacion = detalle.ubicacion_destino
            elif tipo_movimiento == "SALIDA":
                ubicacion = detalle.ubicacion_origen
            else:
                ubicacion = detalle.ubicacion_destino or detalle.ubicacion_origen

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
                "almacen_id": getattr(almacen, "pk", None),
                "almacen_codigo": getattr(almacen, "codigo", None),
                "almacen_nombre": getattr(almacen, "nombre", None),
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
