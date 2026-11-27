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
from inventarios.services.costeo import costos_promedio_al
from nucleo.kpis import entero_estricto, vacio

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
    con ``clave``. Ignorarlo quitaba el filtro en silencio (#805, #815, EC-822).
    Mismo lector estricto que ``entero_param`` (EC-829)."""
    return entero_estricto(valor, clave, positivo=True)


def almacen_del_query(query_params):
    """``almacen``/``almacen_id`` de los reportes por periodo (EC-822): el primer
    alias NO vacío, estricto, y el 400 siempre con la clave ``almacen``. Antes,
    ``almacen=abc&almacen_id=5`` tomaba ``abc`` y se quedaba sin filtro."""
    raw = next((v for v in (query_params.get("almacen"), query_params.get("almacen_id")) if not vacio(v)), None)
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
    # Superusuario: toda su empresa activa, sin sub-alcance por sucursal; nunca
    # otras (#1035, EC-798). ``?empresa=`` ya no elige otra empresa.
    if user.is_superuser:
        from nucleo.scope import empresa_ids_lectura

        return qs.filter(empresa_id__in=empresa_ids_lectura(user))
    return almacenes_en_alcance(user, qs)


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


#: Las transferencias se leen del ledger (``_iter_ledger_sin_bitacora``), no de la
#: bitácora: su evento no trae ``delta`` ni almacén por item y las TR anteriores a
#: ese evento ni lo tienen (#1019, EC-823).
ACCION_TRANSFERENCIA = "TRANSFERENCIA"


def _iter_auditoria_items(eventos, allowed_almacen_ids, producto_id=None, producto_variante_id=None):
    for evento in eventos.exclude(accion=ACCION_TRANSFERENCIA).iterator():
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


def _iter_ledger_sin_bitacora(desde, empresa_ids, allowed_almacen_ids, producto_id=None, producto_variante_id=None):
    """Movimientos del ledger desde ``desde`` que la bitácora no registra (#1019):

    - Transferencias (TR), un renglón por lado: la salida (``-cantidad``) del almacén
      de origen y la entrada del de destino. Almacén de cada lado: el de su ubicación
      o, si no tiene, el de la TR, como en el costo.
    - Salidas por despacho (``MovimientoInventario.despacho``): el despacho no escribe
      evento de existencias. Almacén: el de la ubicación de origen o ``detalle.almacen``.
      Su cancelación es una ENTRADA ligada al mismo despacho, que suma de regreso.

    Cada fila trae su ``fecha`` para repartirla entre el periodo y lo posterior al
    corte. Una consulta, sin importar cuántos movimientos haya."""
    qs = MovimientoInventarioDetalle.objects.filter(
        models.Q(movimiento_inventario__transferencia__isnull=False)
        | models.Q(movimiento_inventario__despacho__isnull=False,
                   movimiento_inventario__tipo_movimiento__in=("SALIDA", "ENTRADA")),
        movimiento_inventario__activo=True,
        movimiento_inventario__empresa_id__in=empresa_ids,
        movimiento_inventario__fecha_movimiento__gte=desde,
    )
    if producto_variante_id:
        qs = qs.filter(producto_variante_id=producto_variante_id)
    if producto_id:
        qs = qs.filter(models.Q(producto_id=producto_id) | models.Q(producto_variante__producto_id=producto_id))
    filas = qs.values_list(
        "movimiento_inventario__fecha_movimiento", "producto_id", "producto_variante_id",
        "producto_variante__producto_id", "cantidad",
        "movimiento_inventario__transferencia_id", "almacen_id",
        "ubicacion_origen__almacen_id", "movimiento_inventario__transferencia__almacen_origen_id",
        "ubicacion_destino__almacen_id", "movimiento_inventario__transferencia__almacen_destino_id",
        "movimiento_inventario__tipo_movimiento",
    )
    for fila in filas.iterator():
        fecha, prod_id, variante_id, variante_prod_id, cantidad, transferencia_id, almacen_detalle = fila[:7]
        ubic_origen, tr_origen, ubic_destino, tr_destino, tipo = fila[7:]
        cantidad = to_decimal(cantidad)
        # Misma llave que ``Existencia``: el producto de la variante si no trae producto.
        producto_key = prod_id or variante_prod_id
        if transferencia_id:
            lados = ((_primero((ubic_origen, tr_origen)), -cantidad), (_primero((ubic_destino, tr_destino)), cantidad))
        elif tipo == "ENTRADA":  # cancelación de un despacho: regresa al almacén
            lados = ((_primero((ubic_destino, almacen_detalle)), cantidad),)
        else:  # salida por despacho
            lados = ((_primero((ubic_origen, almacen_detalle)), -cantidad),)
        for almacen_id, delta in lados:
            if almacen_id in allowed_almacen_ids:
                yield {
                    "fecha": fecha,
                    "almacen_id": almacen_id,
                    "producto_id": producto_key,
                    "producto_variante_id": variante_id,
                    "delta": delta,
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

    # Transferencias y despachos del ledger (#1019): entradas/salidas en el periodo
    # y, después del corte, para reconstruir la existencia final.
    for item in _iter_ledger_sin_bitacora(
        inicio_dt, empresa_ids, allowed_almacen_ids,
        producto_id=producto_id, producto_variante_id=producto_variante_id,
    ):
        key = (item["almacen_id"], item["producto_id"], item["producto_variante_id"])
        delta = item["delta"]
        if item["fecha"] > final_dt:
            post_end_delta_map[key] += delta
        else:
            period_delta_map[key] += delta
            if delta >= 0:
                period_in_map[key] += delta
            else:
                period_out_map[key] += abs(delta)
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

    # Costo oficial = promedio ponderado (#809) al cierre del periodo (EC-815).
    # Antes: el ``costo_unitario`` del último movimiento, casi siempre 0, y un
    # recorrido de todo el historial cuando a una llave le faltaba costo.
    empresa_de_almacen = {aid: row["empresa_id"] for aid, row in almacen_map.items()}
    cost_map = costos_promedio_al(
        {(empresa_de_almacen.get(k[0]), k[1], k[2]) for k in keys}, final_dt,
    )
    cost_map = {k: cost_map.get((empresa_de_almacen.get(k[0]), k[1], k[2]), Decimal("0")) for k in keys}

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


#: ``tipo_movimiento`` vacío o ``TODOS``: los cuatro tipos juntos.
TODOS = "TODOS"


def tipos_del_query(query_params):
    """``(etiqueta, tipos)``: ``TODOS`` (o sin ``tipo_movimiento``) son los cuatro."""
    tipo_movimiento = (query_params.get("tipo_movimiento") or "").strip().upper()
    if tipo_movimiento in ("", TODOS):
        return TODOS, TIPOS_MOVIMIENTO
    if tipo_movimiento not in TIPOS_MOVIMIENTO:
        raise ValidationError(
            {"tipo_movimiento": "tipo_movimiento debe ser ENTRADA, SALIDA, AJUSTE, TRANSFERENCIA o TODOS."}
        )
    return tipo_movimiento, (tipo_movimiento,)


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


def _almacen_por_lado(tipos):
    """Anotaciones ``almacen_origen_res``/``almacen_destino_res`` (id) para uno o
    varios tipos, con las rutas de ``RUTAS_ALMACEN_POR_LADO``. El AJUSTE solo usa
    el lado origen (los dos son el mismo almacén)."""
    anotaciones = {}
    for lado in ("origen", "destino"):
        casos = []
        for tipo in tipos:
            if tipo == "AJUSTE" and lado == "destino":
                continue
            rutas = RUTAS_ALMACEN_POR_LADO[tipo].get(lado)
            if not rutas:
                continue
            ids = [f"{ruta}_id" for ruta in rutas]
            expr = Coalesce(*ids) if len(ids) > 1 else models.F(ids[0])
            casos.append((tipo, expr))
        if not casos:
            continue
        if len(tipos) == 1:
            anotaciones[f"almacen_{lado}_res"] = casos[0][1]
        else:
            anotaciones[f"almacen_{lado}_res"] = models.Case(
                *(models.When(movimiento_inventario__tipo_movimiento=t, then=e) for t, e in casos),
                default=None, output_field=models.BigIntegerField(),
            )
    return anotaciones


#: ``ordering`` permitido -> campo de la consulta (``-`` para descendente).
ORDENAMIENTOS = {
    "fecha": "movimiento_inventario__fecha_movimiento",
    "tipo": "movimiento_inventario__tipo_movimiento",
    "sku": "producto_variante__sku",
    "producto": "producto_variante__producto__nombre",
    "cantidad": "cantidad",
    "costo_unitario": "costo_unitario",
    "usuario": "movimiento_inventario__usuario__email",
    "movimiento": "movimiento_inventario_id",
}


def _ordenamiento(query_params):
    valor = (query_params.get("ordering") or "-fecha").strip()
    campos = []
    for parte in [p.strip() for p in valor.split(",") if p.strip()]:
        desc = parte.startswith("-")
        campo = ORDENAMIENTOS.get(parte.lstrip("-"))
        if not campo:
            raise ValidationError(
                {"ordering": f"ordering admite: {', '.join(ORDENAMIENTOS)} (con - para descendente)."}
            )
        campos.append(f"-{campo}" if desc else campo)
    desc_id = campos[0].startswith("-") if campos else True
    return campos + ["-id" if desc_id else "id"]


def _filtros_movimientos(query_params):
    """Filtros opcionales del reporte de movimientos (ids estrictos: 400 si no son
    enteros positivos)."""
    qp = query_params
    return {
        "sku": (qp.get("sku") or "").strip() or None,
        "producto_id": id_positivo(qp.get("producto_id") or qp.get("producto"), "producto_id"),
        "usuario_id": id_positivo(qp.get("usuario_id") or qp.get("usuario"), "usuario_id"),
        "ubicacion_id": id_positivo(qp.get("ubicacion_id") or qp.get("ubicacion"), "ubicacion_id"),
        "referencia": (qp.get("referencia") or "").strip() or None,
        "q": (qp.get("q") or "").strip() or None,
    }


TIPOS_RESUMEN = ("entradas", "salidas", "ajustes_positivos", "ajustes_negativos", "transferencias")


def _bucket(tipo, signo):
    if tipo == "ENTRADA":
        return "entradas"
    if tipo == "SALIDA":
        return "salidas"
    if tipo == "AJUSTE":
        return "ajustes_negativos" if signo < 0 else "ajustes_positivos"
    return "transferencias"


def _deltas_ajuste_auditoria(empresa_ids, ajuste_ids):
    """(ajuste, variante, ubicación) -> delta, de la bitácora, para ajustes sin
    ``cantidad_antes``/``cantidad_despues`` (anteriores a esos campos). Una consulta."""
    deltas = {}
    if not ajuste_ids:
        return deltas
    eventos = AuditoriaEvento.objects.filter(
        modulo="inventarios", tabla="existencias", accion="AJUSTE",
        empresa_id__in=empresa_ids, despues_json__ajuste_id__in=sorted(ajuste_ids),
    ).values_list("despues_json", flat=True)
    for despues in eventos:
        for item in (despues or {}).get("items") or []:
            clave = (despues.get("ajuste_id"), to_int(item.get("producto_variante_id")), to_int(item.get("ubicacion_id")))
            deltas.setdefault(clave, to_decimal(item.get("delta")))
    return deltas


def _fmt_bucket(b):
    return {"registros": b["registros"], "piezas": str(quantize_qty(b["piezas"])), "costo": str(quantize_money(b["costo"]))}


def _resumen(por_tipo, total_cantidad, neto_piezas, neto_costo, movimientos, registros, transferencias_lados=None):
    resumen = {
        "total_movimientos": movimientos,
        "total_registros": registros,
        "total_cantidad": str(quantize_qty(total_cantidad)),
        "neto_piezas": str(quantize_qty(neto_piezas)),
        "neto_costo": str(quantize_money(neto_costo)),
        "por_tipo": {
            k: {
                "registros": por_tipo[k]["registros"],
                "piezas": str(quantize_qty(por_tipo[k]["piezas"])),
                "costo": str(quantize_money(por_tipo[k]["costo"])),
            }
            for k in TIPOS_RESUMEN
        },
    }
    if transferencias_lados is not None:
        # Con ``almacen``: lo que salió de él y lo que entró a él por transferencia.
        resumen["por_tipo"]["transferencias"].update(
            {lado: _fmt_bucket(b) for lado, b in transferencias_lados.items()}
        )
    return resumen


def _por_tipo_en_cero():
    return {k: {"registros": 0, "piezas": Decimal("0"), "costo": Decimal("0")} for k in TIPOS_RESUMEN}


def _lados_transferencia(detalle, tipo):
    """``almacen_origen_*``/``almacen_destino_*`` de una transferencia (null en otros tipos)."""
    datos = {}
    for lado in ("origen", "destino"):
        es_tr = tipo == "TRANSFERENCIA"
        datos[f"almacen_{lado}_id"] = getattr(detalle, f"almacen_{lado}_res", None) if es_tr else None
        datos[f"almacen_{lado}_nombre"] = getattr(detalle, f"tr_{lado}_nombre", None) if es_tr else None
    return datos


_UBICACION = ("id_ubicacion", "pasillo", "rack", "nivel", "posicion", "almacen_id")
_COLUMNAS_MOVIMIENTOS = (
    "id", "cantidad", "costo_unitario", "cantidad_antes", "cantidad_despues",
    "producto_id", "producto_variante_id", "variante_produccion_id",
    "ubicacion_origen_id", "ubicacion_destino_id", "movimiento_inventario_id",
    *(f"movimiento_inventario__{c}" for c in (
        "id", "tipo_movimiento", "fecha_movimiento", "observaciones", "ajuste_inventario_id", "usuario_id",
    )),
    *(f"movimiento_inventario__usuario__{c}" for c in ("id", "first_name", "last_name", "email")),
    *(f"movimiento_inventario__ajuste_inventario__{c}" for c in ("id", "observaciones", "motivo")),
    "producto__id", "producto__nombre",
    *(f"producto_variante__{c}" for c in ("id", "sku", "nombre", "producto_id", "color_id", "talla_id")),
    "producto_variante__producto__id", "producto_variante__producto__nombre",
    "producto_variante__color__id", "producto_variante__color__nombre",
    "producto_variante__talla__id", "producto_variante__talla__nombre",
    *(f"variante_produccion__{c}" for c in ("id", "sku", "nombre")),
    *(f"ubicacion_origen__{c}" for c in _UBICACION),
    *(f"ubicacion_destino__{c}" for c in _UBICACION),
    "ubicacion_origen__almacen__id_almacen", "ubicacion_origen__almacen__nombre",
    "ubicacion_destino__almacen__id_almacen", "ubicacion_destino__almacen__nombre",
    "lote_id", "movimiento_inventario__ajuste_inventario__motivo_clave",
)

M_ = "movimiento_inventario__"
#: Tercero y tipo de producto, anotados en la misma consulta del detalle.
_ANOTACIONES_CONTABLES = {
    "c_cliente_id": Coalesce(f"{M_}pedido__cliente_id", f"{M_}factura__cliente_id"),
    "c_cliente_nombre": Coalesce(
        models.functions.NullIf(f"{M_}pedido__cliente__razon_social", models.Value("")),
        f"{M_}pedido__cliente__nombre",
        models.functions.NullIf(f"{M_}factura__cliente__razon_social", models.Value("")),
        f"{M_}factura__cliente__nombre",
    ),
    "c_proveedor_id": Coalesce(f"{M_}recepcion__proveedor_id", f"{M_}recepcion__orden_compra__proveedor_id"),
    "c_proveedor_nombre": Coalesce(
        f"{M_}recepcion__proveedor__nombre", f"{M_}recepcion__orden_compra__proveedor__nombre",
    ),
    "c_tipo_producto": Coalesce(
        "producto_variante__producto__tipo__codigo", "producto__tipo__codigo",
    ),
}

#: Categoría contable: el almacén en proceso manda; si no, el tipo del producto
#: (MP/PT) y, sin tipo, el del almacén.
CATEGORIAS_INVENTARIO = ("MATERIA_PRIMA", "EN_PROCESO", "PRODUCTO_TERMINADO")


def categoria_inventario(tipo_almacen, tipo_producto):
    if tipo_almacen == "PROCESO":
        return "EN_PROCESO"
    if tipo_producto == "PT":
        return "PRODUCTO_TERMINADO"
    if tipo_producto == "MP":
        return "MATERIA_PRIMA"
    return {"PT": "PRODUCTO_TERMINADO", "MP": "MATERIA_PRIMA"}.get(tipo_almacen)


def _sub(qs, campo):
    return models.Subquery(qs.values(campo)[:1])


def anotar_documentos(qs):
    """Factura (de proveedor por recepción; de cliente por factura o, si no, la
    más reciente del pedido), su póliza y el costo antes/después del renglón,
    como subconsultas de la consulta del detalle: cero viajes extra a la BD.
    Todas van por FK o índice (``ix_poliza_origen``)."""
    from finanzas.models import Factura, FacturaProveedor, Poliza
    from inventarios.models import HistorialCostoInventario

    vigente = ~models.Q(estatus="Cancelada")
    fp = FacturaProveedor.objects.filter(vigente, recepcion_id=models.OuterRef(f"{M_}recepcion_id")).order_by("-id")
    fc = Coalesce(
        _sub(Factura.objects.filter(vigente, pk=models.OuterRef(f"{M_}factura_id")), "id"),
        _sub(Factura.objects.filter(vigente, pedido_id=models.OuterRef(f"{M_}pedido_id")).order_by("-id"), "id"),
    )
    hist = HistorialCostoInventario.objects.filter(movimiento_detalle_id=models.OuterRef("pk")).order_by("-id")
    qs = qs.annotate(
        d_fp_id=_sub(fp, "id"), d_fp_folio=_sub(fp, "folio"), d_fc_id=fc,
        d_costo_antes=_sub(hist, "costo_anterior"), d_costo_despues=_sub(hist, "costo_nuevo"),
    )
    fc_de = Factura.objects.filter(pk=models.OuterRef("d_fc_id"))

    def poliza(origen_tipo, ref):
        return Poliza.objects.filter(
            vigente, origen_tipo=origen_tipo, origen_id=models.OuterRef(ref),
        ).order_by("-id")

    return qs.annotate(
        d_fc_folio=_sub(fc_de, "folio"), d_fc_uuid=_sub(fc_de, "cfdi_uuid"),
        d_pz_fp_id=_sub(poliza("FacturaProveedor", "d_fp_id"), "id"),
        d_pz_fp_folio=_sub(poliza("FacturaProveedor", "d_fp_id"), "folio"),
        d_pz_fc_id=_sub(poliza("Factura", "d_fc_id"), "id"),
        d_pz_fc_folio=_sub(poliza("Factura", "d_fc_id"), "folio"),
    )


def documentos_de(detalle):
    """``(factura, poliza, factura_tipo)`` del renglón, de ``anotar_documentos``."""
    if detalle.d_fp_id:
        return (
            {"id": detalle.d_fp_id, "folio": detalle.d_fp_folio, "cfdi_uuid": None},
            {"id": detalle.d_pz_fp_id, "folio": detalle.d_pz_fp_folio} if detalle.d_pz_fp_id else None,
            "PROVEEDOR",
        )
    if detalle.d_fc_id:
        return (
            {"id": detalle.d_fc_id, "folio": detalle.d_fc_folio, "cfdi_uuid": detalle.d_fc_uuid},
            {"id": detalle.d_pz_fc_id, "folio": detalle.d_pz_fc_folio} if detalle.d_pz_fc_id else None,
            "CLIENTE",
        )
    return None, None, None


def _alcance_movimientos(user, query_params):
    """Almacenes en alcance (con código, nombre y tipo) y el ``almacen`` pedido,
    en una sola consulta: misma regla que ``almacen_ids_movimientos``."""
    from nucleo.scope import empresa_ids_lectura

    almacen_id = almacen_del_query(query_params)
    qs = Almacen.objects.filter(empresa_id__in=empresa_ids_lectura(user))
    if not user.is_superuser:
        # = ``almacenes_en_alcance``, con las sucursales del usuario como
        # subconsulta (M2M ∪ ``sucursal_default``): un viaje menos a la BD.
        sucursales = models.Q(sucursal_id__in=user.sucursales.values("pk"))
        if user.sucursal_default_id:
            sucursales |= models.Q(sucursal_id=user.sucursal_default_id)
        qs = qs.filter(sucursales)
    if almacen_id:
        qs = qs.filter(pk=almacen_id)
    return {a["id_almacen"]: a for a in qs.values("id_almacen", "codigo", "nombre", "tipo_almacen")}, almacen_id


def reporte_movimientos(user, query_params):
    """Reporte de movimientos del periodo, completo y sin paginar.

    ``tipo_movimiento`` opcional (vacío o ``TODOS`` = los cuatro). Regresa
    ``tipo_movimiento``, ``fecha_inicio``/``fecha_final`` (``date``),
    ``almacen_id`` (el filtro pedido), ``filtros``, ``sin_almacenes``,
    ``results`` y ``resumen`` (con ``por_tipo``) con el mismo formato que la
    respuesta JSON. Consultas fijas: alcance, almacenes, detalle y, si hay
    ajustes viejos sin cantidades antes/después, la bitácora.
    """
    from nucleo.scope import empresa_ids_lectura

    from .origen import anotar_origen, filtro_busqueda, filtro_referencia, resolver_origen

    etiqueta, tipos = tipos_del_query(query_params)
    fecha_inicio, fecha_final, inicio_dt, final_dt = parse_fechas(query_params)
    filtros = _filtros_movimientos(query_params)
    ordenamiento = _ordenamiento(query_params)
    almacenes, almacen_id = _alcance_movimientos(user, query_params)
    allowed_almacen_ids = set(almacenes)

    reporte = {
        "tipo_movimiento": etiqueta,
        "fecha_inicio": fecha_inicio,
        "fecha_final": fecha_final,
        "almacen_id": almacen_id,
        "filtros": {"almacen_id": almacen_id, "tipo_movimiento": etiqueta, **filtros},
        "sin_almacenes": not allowed_almacen_ids,
        "results": [],
        "resumen": _resumen(_por_tipo_en_cero(), 0, 0, 0, 0, 0),
    }
    if not allowed_almacen_ids:
        return reporte

    empresa_ids = empresa_ids_lectura(user)

    detalles_qs = (
        MovimientoInventarioDetalle.objects.select_related(
            "movimiento_inventario",
            "movimiento_inventario__usuario",
            # Observaciones/motivo del ajuste.
            "movimiento_inventario__ajuste_inventario",
            "producto",
            "producto_variante",
            "producto_variante__producto",
            "producto_variante__color",
            "producto_variante__talla",
            "variante_produccion",
            # ``str(ubicacion)`` lleva el nombre de su almacén.
            "ubicacion_origen__almacen",
            "ubicacion_destino__almacen",
        )
        # Solo las columnas que se usan: las filas completas de producto y
        # variante multiplicaban lo que viaja desde la BD.
        .only(*_COLUMNAS_MOVIMIENTOS)
        .filter(
            movimiento_inventario__activo=True,
            movimiento_inventario__fecha_movimiento__gte=inicio_dt,
            movimiento_inventario__fecha_movimiento__lte=final_dt,
            # Defensa: además del almacén, el movimiento debe ser de la empresa
            # activa del usuario, superusuario incluido (como el auxiliar).
            movimiento_inventario__empresa_id__in=empresa_ids,
        )
    )
    if len(tipos) == 1:
        detalles_qs = detalles_qs.filter(movimiento_inventario__tipo_movimiento=tipos[0])

    if filtros["sku"]:
        detalles_qs = detalles_qs.filter(
            models.Q(producto_variante__sku=filtros["sku"]) | models.Q(variante_produccion__sku=filtros["sku"])
        )
    if filtros["producto_id"]:
        detalles_qs = detalles_qs.filter(
            models.Q(producto_id=filtros["producto_id"]) | models.Q(producto_variante__producto_id=filtros["producto_id"])
        )
    if filtros["usuario_id"]:
        detalles_qs = detalles_qs.filter(movimiento_inventario__usuario_id=filtros["usuario_id"])
    if filtros["ubicacion_id"]:
        detalles_qs = detalles_qs.filter(
            models.Q(ubicacion_origen_id=filtros["ubicacion_id"]) | models.Q(ubicacion_destino_id=filtros["ubicacion_id"])
        )
    if filtros["referencia"]:
        detalles_qs = detalles_qs.filter(filtro_referencia(filtros["referencia"]))
    if filtros["q"]:
        detalles_qs = detalles_qs.filter(filtro_busqueda(filtros["q"]))

    # Un renglón entra si alguno de sus lados está en alcance, y se muestra con
    # el primero que lo esté (origen antes que destino). Una fila por renglón.
    anotaciones = _almacen_por_lado(tipos)
    filtro = models.Q()
    for nombre in anotaciones:
        filtro |= models.Q(**{f"{nombre}__in": allowed_almacen_ids})
    detalles_qs = (
        anotar_origen(detalles_qs.annotate(**anotaciones).filter(filtro))
        .annotate(**_ANOTACIONES_CONTABLES)
        .order_by(*ordenamiento)
    )
    detalles_qs = anotar_documentos(detalles_qs)
    if "TRANSFERENCIA" in tipos:
        # Nombre del almacén de cada lado (el otro lado puede estar fuera del alcance).
        tr = models.Q(movimiento_inventario__tipo_movimiento="TRANSFERENCIA")
        detalles_qs = detalles_qs.annotate(**{
            f"tr_{lado}_nombre": models.Case(
                models.When(tr, then=_primer_almacen(RUTAS_ALMACEN_POR_LADO["TRANSFERENCIA"][lado], "nombre")),
                default=None, output_field=models.CharField(),
            )
            for lado in ("origen", "destino")
        })

    detalles = list(detalles_qs)
    # Ajustes viejos sin cantidad antes/después: delta de la bitácora (1 consulta).
    deltas_bitacora = _deltas_ajuste_auditoria(empresa_ids, {
        d.movimiento_inventario.ajuste_inventario_id for d in detalles
        if d.movimiento_inventario.tipo_movimiento == "AJUSTE" and d.movimiento_inventario.ajuste_inventario_id
        and (d.cantidad_antes is None or d.cantidad_despues is None)
    })

    por_motivo = defaultdict(lambda: {"registros": 0, "piezas": Decimal("0"), "costo": Decimal("0")})
    por_categoria = defaultdict(lambda: {"registros": 0, "piezas": Decimal("0"), "costo": Decimal("0")})

    resultados = []
    total_cantidad = neto_piezas = neto_costo = Decimal("0")
    por_tipo = _por_tipo_en_cero()
    transferencias_lados = (
        {lado: {"registros": 0, "piezas": Decimal("0"), "costo": Decimal("0")} for lado in ("salientes", "entrantes")}
        if almacen_id else None
    )
    movimiento_ids = set()

    for detalle in detalles:
        movimiento = detalle.movimiento_inventario
        tipo = movimiento.tipo_movimiento
        variante = detalle.producto_variante
        producto = getattr(variante, "producto", None) if variante else detalle.producto

        origen_id = getattr(detalle, "almacen_origen_res", None)
        if origen_id in allowed_almacen_ids:
            lado, almacen_fila_id = "origen", origen_id
        else:
            lado, almacen_fila_id = "destino", getattr(detalle, "almacen_destino_res", None)
        almacen = almacenes.get(almacen_fila_id, {})
        if tipo == "AJUSTE":
            ubicacion = detalle.ubicacion_destino or detalle.ubicacion_origen
        else:
            ubicacion = detalle.ubicacion_origen if lado == "origen" else detalle.ubicacion_destino

        cantidad = to_decimal(detalle.cantidad)
        costo_unitario = to_decimal(detalle.costo_unitario)
        if tipo == "ENTRADA":
            signed = cantidad
        elif tipo == "SALIDA":
            signed = -cantidad
        elif tipo == "TRANSFERENCIA":
            # Sin ``almacen``: la pieza no sale de la empresa, efecto 0 y fuera
            # del neto. Con ``almacen``: − si sale de él, + si entra.
            if not almacen_id:
                signed = Decimal("0")
            else:
                signed = -cantidad if lado == "origen" else cantidad
        elif detalle.cantidad_antes is not None and detalle.cantidad_despues is not None:
            # AJUSTE: ``cantidad`` es la cantidad final; el efecto es la diferencia.
            signed = to_decimal(detalle.cantidad_despues) - to_decimal(detalle.cantidad_antes)
        else:
            signed = deltas_bitacora.get(
                (movimiento.ajuste_inventario_id, detalle.producto_variante_id, getattr(ubicacion, "pk", None)),
                cantidad,
            )
        costo_signed = quantize_money(signed * costo_unitario)

        def o(campo, _d=detalle):
            return getattr(_d, f"o_{campo}")

        origen_tipo, origen_folio, origen_doc_id = resolver_origen(tipo, movimiento.pk, o)

        total_cantidad += cantidad
        neto_piezas += signed
        neto_costo += costo_signed
        buckets = [por_tipo[_bucket(tipo, signed)]]
        if tipo == "TRANSFERENCIA" and transferencias_lados is not None:
            buckets.append(transferencias_lados["salientes" if lado == "origen" else "entrantes"])
        # Piezas y costo movidos (sin signo); en la transferencia sin almacén el
        # efecto es 0 pero sí se movieron.
        for bucket in buckets:
            bucket["registros"] += 1
            bucket["piezas"] += cantidad if tipo == "TRANSFERENCIA" else abs(signed)
            bucket["costo"] += abs(quantize_money(cantidad * costo_unitario)) if tipo == "TRANSFERENCIA" else abs(costo_signed)
        movimiento_ids.add(movimiento.pk)

        usuario = movimiento.usuario
        ajuste = movimiento.ajuste_inventario
        vp = detalle.variante_produccion

        categoria = categoria_inventario(almacen.get("tipo_almacen"), detalle.c_tipo_producto)
        motivo_clave = getattr(ajuste, "motivo_clave", None) if tipo == "AJUSTE" else None
        for grupo, llave in ((por_categoria, categoria or "SIN_CATEGORIA"), (por_motivo, motivo_clave)):
            if llave:
                grupo[llave]["registros"] += 1
                grupo[llave]["piezas"] += signed
                grupo[llave]["costo"] += costo_signed
        factura, poliza, factura_tipo = documentos_de(detalle)
        # Saldo de la fila afectada (ubicación, o el almacén si no hay ubicación)
        # guardado al mover; null en transferencias y movimientos anteriores.
        antes, despues = detalle.cantidad_antes, detalle.cantidad_despues
        if detalle.d_costo_antes is not None:
            costo_antes, costo_despues = detalle.d_costo_antes, detalle.d_costo_despues
        else:
            costo_antes = costo_despues = costo_unitario
        resultados.append(
            {
                "movimiento_inventario_id": movimiento.pk,
                "movimiento_detalle_id": detalle.pk,
                "tipo_movimiento": tipo,
                "fecha_movimiento": movimiento.fecha_movimiento,
                "almacen_id": almacen_fila_id,
                "almacen_codigo": almacen.get("codigo"),
                "almacen_nombre": almacen.get("nombre"),
                "ubicacion_id": getattr(ubicacion, "pk", None),
                "ubicacion_nombre": str(ubicacion) if ubicacion else None,
                **_lados_transferencia(detalle, tipo),
                "ubicacion_origen_id": detalle.ubicacion_origen_id,
                "ubicacion_origen_nombre": str(detalle.ubicacion_origen) if detalle.ubicacion_origen_id else None,
                "ubicacion_destino_id": detalle.ubicacion_destino_id,
                "ubicacion_destino_nombre": str(detalle.ubicacion_destino) if detalle.ubicacion_destino_id else None,
                "saldo_antes": None if antes is None else str(quantize_qty(antes)),
                "saldo_despues": None if despues is None else str(quantize_qty(despues)),
                "saldo_nivel": None if despues is None else ("UBICACION" if ubicacion else "ALMACEN"),
                "saldo_negativo": None if despues is None else despues < 0,
                "valor_antes": None if antes is None else str(quantize_money(to_decimal(antes) * to_decimal(costo_antes))),
                "valor_despues": None if despues is None else str(quantize_money(to_decimal(despues) * to_decimal(costo_despues))),
                "cliente_id": detalle.c_cliente_id,
                "cliente_nombre": detalle.c_cliente_nombre,
                "proveedor_id": detalle.c_proveedor_id,
                "proveedor_nombre": detalle.c_proveedor_nombre,
                "lote_id": detalle.lote_id,
                "categoria_inventario": categoria,
                "factura_tipo": factura_tipo,
                "factura_id": factura["id"] if factura else None,
                "factura_folio": factura["folio"] if factura else None,
                "factura_uuid": factura["cfdi_uuid"] if factura else None,
                "poliza_id": poliza["id"] if poliza else None,
                "poliza_folio": poliza["folio"] if poliza else None,
                "motivo_ajuste_clave": motivo_clave,
                "producto_id": getattr(producto, "pk", None),
                "producto_variante_id": getattr(variante, "pk", None),
                "sku": getattr(variante, "sku", None) or getattr(vp, "sku", None),
                "producto_nombre": (
                    getattr(variante, "nombre", None) or getattr(producto, "nombre", None) or getattr(vp, "nombre", None)
                ),
                "producto_base_nombre": getattr(producto, "nombre", None),
                "color": getattr(getattr(variante, "color", None), "nombre", None) if variante else None,
                "talla": getattr(getattr(variante, "talla", None), "nombre", None) if variante else None,
                "cantidad": str(cantidad.quantize(Decimal("0.0001"))),
                "cantidad_signed": str(signed.quantize(Decimal("0.0001"))),
                "costo_unitario": str(costo_unitario.quantize(Decimal("0.01"))),
                "costo_total": str(costo_signed),
                "origen_tipo": origen_tipo,
                "origen_folio": origen_folio,
                "origen_id": origen_doc_id,
                "pedido_id": o("pedido_id"),
                "pedido_folio": o("pedido_folio"),
                "transferencia_id": o("transferencia_id"),
                "transferencia_folio": o("transferencia_folio"),
                "recepcion_id": o("recepcion_id"),
                "recepcion_folio": o("recepcion_folio"),
                "ajuste_inventario_id": o("ajuste_id"),
                "op_id": o("op_id"),
                "op_folio": o("op_folio"),
                "usuario_id": getattr(usuario, "pk", None),
                "usuario_nombre": (usuario.get_full_name().strip() or usuario.email) if usuario else None,
                "observaciones": movimiento.observaciones,
                "comentarios": getattr(ajuste, "observaciones", None) or movimiento.observaciones,
                "motivo_ajuste": getattr(ajuste, "motivo", None),
            }
        )

    reporte["results"] = resultados
    reporte["resumen"] = _resumen(
        por_tipo, total_cantidad, neto_piezas, neto_costo, len(movimiento_ids), len(resultados),
        transferencias_lados,
    )
    reporte["resumen"]["por_categoria"] = {k: _fmt_bucket(v) for k, v in sorted(por_categoria.items())}
    reporte["resumen"]["por_motivo_ajuste"] = {k: _fmt_bucket(v) for k, v in sorted(por_motivo.items())}
    return reporte
