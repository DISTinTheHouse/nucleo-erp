"""Auxiliar de movimientos (kardex) de una variante.

Una sola consulta para el front: ``?q=`` busca por SKU (catálogo o de
producción) o descripción; si hay una sola coincidencia (o un SKU exacto, o
``producto_variante``) regresa sus movimientos, si no regresa ``opciones``
para elegir una.

Cada detalle de ``MovimientoInventarioDetalle`` se convierte en filas por
almacén afectado: la transferencia da una salida (origen) y una entrada
(destino); sin ubicaciones, los almacenes salen de la transferencia. El
AJUSTE guarda la cantidad final de la ubicación, no la diferencia: la
diferencia sale de su evento de auditoría (antes/después de la existencia) y,
si no hay evento, del saldo corrido.

Alcance: el de los reportes hermanos (empresa + sucursales permitidas, por
almacén). Un renglón sin almacén se ve si el movimiento es de una sucursal
permitida.
"""
from collections import defaultdict
from decimal import Decimal

from django.db.models import OuterRef, Q, Subquery, Sum

from auditoria.models import AuditoriaEvento
from catalogo.models import ProductoVariante, VarianteProductoProduccion
from inventarios.models import Existencia, MovimientoInventarioDetalle

CERO = Decimal("0")
LIMITE_OPCIONES = 20


def _d(valor):
    return Decimal(str(valor or 0))


def variantes_scope(user):
    qs = ProductoVariante.objects.all()
    if user.is_superuser:
        return qs
    if getattr(user, "empresa_id", None):
        return qs.filter(empresa_id=user.empresa_id)
    return qs.none()


def sku_produccion_subquery():
    return Subquery(
        VarianteProductoProduccion.objects.filter(
            activo=True,
            aplica_catalogo=True,
            producto_base=OuterRef("producto"),
            color=OuterRef("color"),
            talla=OuterRef("talla"),
        ).values("sku")[:1]
    )


def buscar_variantes(user, q):
    """Coincidencias de ``q``; trae una de más que ``LIMITE_OPCIONES`` para
    saber si hay más (``hay_mas``)."""
    qs = (
        variantes_scope(user)
        .annotate(sku_produccion=sku_produccion_subquery())
        .values("id", "sku", "nombre", "producto__nombre", "color__nombre", "talla__nombre", "activo", "sku_produccion")
    )
    exacta = qs.filter(Q(sku__iexact=q) | Q(sku_produccion__iexact=q))[:2]
    if len(exacta) == 1:
        return list(exacta)
    filtro = Q()
    for palabra in q.split():
        filtro &= (
            Q(sku__icontains=palabra)
            | Q(sku_produccion__icontains=palabra)
            | Q(nombre__icontains=palabra)
            | Q(producto__nombre__icontains=palabra)
        )
    return list(qs.filter(filtro).order_by("-activo", "sku")[:LIMITE_OPCIONES + 1])


def opcion(v):
    return {
        "producto_variante_id": v["id"],
        "sku": v["sku"],
        "sku_produccion": v["sku_produccion"],
        "descripcion": v["nombre"] or v["producto__nombre"],
        "color": v["color__nombre"],
        "talla": v["talla__nombre"],
        "activo": v["activo"],
    }


def _documento(m):
    if m["movimiento_inventario__recepcion_id"]:
        return "RECEPCION", m["movimiento_inventario__recepcion__folio"]
    if m["movimiento_inventario__transferencia_id"]:
        return "TRANSFERENCIA", m["movimiento_inventario__transferencia__folio"]
    if m["movimiento_inventario__op_id"]:
        return "ORDEN_PRODUCCION", m["movimiento_inventario__op__folio_op"]
    if m["movimiento_inventario__ajuste_inventario_id"]:
        return "AJUSTE", f"AJ-{m['movimiento_inventario__ajuste_inventario_id']}"
    if m["movimiento_inventario__pedido_id"]:
        return "PEDIDO", m["movimiento_inventario__pedido__folio"]
    return "MOVIMIENTO_MANUAL", None


def _deltas_ajuste(user, ajuste_ids):
    """(ajuste, variante, ubicación) -> diferencias en el orden del ajuste.

    ``cantidad_before`` de la auditoría ya incluye el stock cargado sin
    movimiento, que el saldo corrido no conoce (#802).
    """
    deltas = defaultdict(list)
    if not ajuste_ids:
        return deltas
    eventos = AuditoriaEvento.objects.filter(
        modulo="inventarios", tabla="existencias", accion="AJUSTE",
        despues_json__ajuste_id__in=sorted(ajuste_ids),
    ).order_by("id_evento")
    if not user.is_superuser:
        eventos = eventos.filter(empresa_id=user.empresa_id)
    for despues in eventos.values_list("despues_json", flat=True):
        for item in (despues or {}).get("items") or []:
            clave = (despues.get("ajuste_id"), item.get("producto_variante_id"), item.get("ubicacion_id"))
            deltas[clave].append(_d(item.get("delta")))
    return deltas


def kardex(user, variante_id, almacen_id=None, inicio_dt=None, final_dt=None, alcance=None):
    """Filas del auxiliar en orden cronológico con saldo y costo promedio.

    ``alcance``: ``None`` sin restricción (superuser) o ``(almacen_ids,
    sucursal_ids)`` permitidos.
    """
    detalles = (
        MovimientoInventarioDetalle.objects.filter(
            producto_variante_id=variante_id, movimiento_inventario__activo=True
        )
        .order_by("movimiento_inventario__fecha_movimiento", "id")
        .values(
            "id",
            "cantidad",
            "costo_unitario",
            "ubicacion_origen_id",
            "ubicacion_origen__almacen_id",
            "ubicacion_origen__almacen__codigo",
            "ubicacion_origen__almacen__nombre",
            "ubicacion_destino_id",
            "ubicacion_destino__almacen_id",
            "ubicacion_destino__almacen__codigo",
            "ubicacion_destino__almacen__nombre",
            "movimiento_inventario_id",
            "movimiento_inventario__sucursal_id",
            "movimiento_inventario__tipo_movimiento",
            "movimiento_inventario__fecha_movimiento",
            "movimiento_inventario__observaciones",
            "movimiento_inventario__usuario__first_name",
            "movimiento_inventario__usuario__last_name",
            "movimiento_inventario__usuario__email",
            "movimiento_inventario__recepcion_id",
            "movimiento_inventario__recepcion__folio",
            "movimiento_inventario__recepcion__almacen_id",
            "movimiento_inventario__recepcion__almacen__codigo",
            "movimiento_inventario__recepcion__almacen__nombre",
            "movimiento_inventario__transferencia_id",
            "movimiento_inventario__transferencia__folio",
            "movimiento_inventario__transferencia__almacen_origen_id",
            "movimiento_inventario__transferencia__almacen_origen__codigo",
            "movimiento_inventario__transferencia__almacen_origen__nombre",
            "movimiento_inventario__transferencia__almacen_destino_id",
            "movimiento_inventario__transferencia__almacen_destino__codigo",
            "movimiento_inventario__transferencia__almacen_destino__nombre",
            "movimiento_inventario__op_id",
            "movimiento_inventario__op__folio_op",
            "movimiento_inventario__ajuste_inventario_id",
            "movimiento_inventario__ajuste_inventario__almacen_id",
            "movimiento_inventario__ajuste_inventario__almacen__codigo",
            "movimiento_inventario__ajuste_inventario__almacen__nombre",
            "movimiento_inventario__pedido_id",
            "movimiento_inventario__pedido__folio",
        )
    )
    if not user.is_superuser:
        detalles = detalles.filter(movimiento_inventario__empresa_id=user.empresa_id)
    detalles = list(detalles)
    deltas_ajuste = _deltas_ajuste(user, {
        m["movimiento_inventario__ajuste_inventario_id"] for m in detalles
        if m["movimiento_inventario__tipo_movimiento"] == "AJUSTE" and m["movimiento_inventario__ajuste_inventario_id"]
    })

    def visible(alm_id, sucursal_id):
        if alcance is None:
            return True
        almacenes, sucursales = alcance
        return alm_id in almacenes if alm_id else sucursal_id in sucursales

    saldo_ubicacion = defaultdict(lambda: CERO)  # (almacen, ubicacion) -> cantidad
    saldo_almacen = defaultdict(lambda: CERO)
    saldo_total = CERO
    costo_promedio = CERO
    saldo_inicial = CERO
    filas = []

    for m in detalles:
        tipo = m["movimiento_inventario__tipo_movimiento"]
        cantidad = _d(m["cantidad"])
        costo = _d(m["costo_unitario"])
        afectaciones = []  # (almacen, codigo, nombre, ubicacion, delta)

        if tipo == "AJUSTE":
            ub = m["ubicacion_destino_id"] or m["ubicacion_origen_id"]
            if m["ubicacion_destino__almacen_id"]:
                alm = (m["ubicacion_destino__almacen_id"], m["ubicacion_destino__almacen__codigo"], m["ubicacion_destino__almacen__nombre"])
            elif m["ubicacion_origen__almacen_id"]:
                alm = (m["ubicacion_origen__almacen_id"], m["ubicacion_origen__almacen__codigo"], m["ubicacion_origen__almacen__nombre"])
            else:
                alm = (
                    m["movimiento_inventario__ajuste_inventario__almacen_id"],
                    m["movimiento_inventario__ajuste_inventario__almacen__codigo"],
                    m["movimiento_inventario__ajuste_inventario__almacen__nombre"],
                )
            pendientes = deltas_ajuste.get((m["movimiento_inventario__ajuste_inventario_id"], variante_id, ub))
            delta = pendientes.pop(0) if pendientes else cantidad - saldo_ubicacion[(alm[0], ub)]
            afectaciones.append((*alm, ub, delta))
        else:
            transferencia = tipo == "TRANSFERENCIA"
            if m["ubicacion_origen_id"]:
                afectaciones.append((
                    m["ubicacion_origen__almacen_id"], m["ubicacion_origen__almacen__codigo"],
                    m["ubicacion_origen__almacen__nombre"], m["ubicacion_origen_id"], -cantidad,
                ))
            elif transferencia and m["movimiento_inventario__transferencia__almacen_origen_id"]:
                # Sin ubicaciones (picking, almacenes sin ubicaciones) el
                # almacén sale de la transferencia (#801).
                afectaciones.append((
                    m["movimiento_inventario__transferencia__almacen_origen_id"],
                    m["movimiento_inventario__transferencia__almacen_origen__codigo"],
                    m["movimiento_inventario__transferencia__almacen_origen__nombre"], None, -cantidad,
                ))
            if m["ubicacion_destino_id"]:
                afectaciones.append((
                    m["ubicacion_destino__almacen_id"], m["ubicacion_destino__almacen__codigo"],
                    m["ubicacion_destino__almacen__nombre"], m["ubicacion_destino_id"], cantidad,
                ))
            elif transferencia and m["movimiento_inventario__transferencia__almacen_destino_id"]:
                afectaciones.append((
                    m["movimiento_inventario__transferencia__almacen_destino_id"],
                    m["movimiento_inventario__transferencia__almacen_destino__codigo"],
                    m["movimiento_inventario__transferencia__almacen_destino__nombre"], None, cantidad,
                ))
            if not afectaciones:
                if tipo == "ENTRADA" and m["movimiento_inventario__recepcion__almacen_id"]:
                    afectaciones.append((
                        m["movimiento_inventario__recepcion__almacen_id"],
                        m["movimiento_inventario__recepcion__almacen__codigo"],
                        m["movimiento_inventario__recepcion__almacen__nombre"], None, cantidad,
                    ))
                else:
                    afectaciones.append((None, None, None, None, -cantidad if tipo == "SALIDA" else cantidad))

        documento, folio = _documento(m)
        usuario = (
            f"{m['movimiento_inventario__usuario__first_name'] or ''} {m['movimiento_inventario__usuario__last_name'] or ''}".strip()
            or m["movimiento_inventario__usuario__email"]
        )
        fecha = m["movimiento_inventario__fecha_movimiento"]

        for alm_id, alm_codigo, alm_nombre, ub, delta in afectaciones:
            if not visible(alm_id, m["movimiento_inventario__sucursal_id"]):
                continue
            # Costo promedio ponderado: solo una entrada con costo lo revalúa.
            if delta > 0 and costo > 0 and saldo_total + delta > 0:
                costo_promedio = (max(saldo_total, CERO) * costo_promedio + delta * costo) / (max(saldo_total, CERO) + delta)
            saldo_ubicacion[(alm_id, ub)] += delta
            saldo_almacen[alm_id] += delta
            saldo_total += delta

            if almacen_id and alm_id != almacen_id:
                continue
            saldo = saldo_almacen[alm_id] if almacen_id else saldo_total
            if inicio_dt and fecha and fecha < inicio_dt:
                saldo_inicial = saldo
                continue
            if final_dt and fecha and fecha > final_dt:
                continue
            filas.append({
                "movimiento_inventario_id": m["movimiento_inventario_id"],
                "movimiento_detalle_id": m["id"],
                "fecha": fecha,
                "documento": documento,
                "folio_referencia": folio,
                "tm": tipo,
                "costo_unitario": str(costo.quantize(Decimal("0.01"))),
                "entradas": str(delta.quantize(Decimal("0.0001"))) if delta > 0 else "0.0000",
                "salidas": str((-delta).quantize(Decimal("0.0001"))) if delta < 0 else "0.0000",
                "efecto": "ENTRADA" if delta > 0 else ("SALIDA" if delta < 0 else "SIN_CAMBIO"),
                "saldo_almacen": saldo_almacen[alm_id],
                "saldo": saldo,
                "almacen_id": alm_id,
                "almacen_codigo": alm_codigo,
                "almacen_nombre": alm_nombre,
                "usuario": usuario,
                "costo_promedio": str(costo_promedio.quantize(Decimal("0.01"))),
                "observaciones": m["movimiento_inventario__observaciones"],
            })

    # Stock cargado sin movimiento (carga inicial, importaciones): se suma
    # como saldo de arranque por almacén para que el saldo final cuadre con
    # la existencia real.
    existencias = Existencia.objects.filter(producto_variante_id=variante_id)
    if not user.is_superuser:
        existencias = existencias.filter(almacen__empresa_id=user.empresa_id)
    if alcance is not None:
        existencias = existencias.filter(almacen_id__in=alcance[0])
    real = {e["almacen_id"]: _d(e["t"]) for e in existencias.values("almacen_id").annotate(t=Sum("cantidad"))}
    sin_mov = {a: real.get(a, CERO) - saldo_almacen.get(a, CERO) for a in set(real) | set(saldo_almacen)}
    if almacen_id:
        sin_mov_total = sin_mov.get(almacen_id, CERO)
        existencia_actual = real.get(almacen_id, CERO)
    else:
        sin_mov_total = sum(sin_mov.values(), CERO)
        existencia_actual = sum(real.values(), CERO)
    saldo_inicial += sin_mov_total

    q4 = Decimal("0.0001")
    for f in filas:
        f["saldo_almacen"] = str((f["saldo_almacen"] + sin_mov.get(f["almacen_id"], CERO)).quantize(q4))
        f["saldo"] = str((f["saldo"] + sin_mov_total).quantize(q4))

    total_entradas = sum((_d(f["entradas"]) for f in filas), CERO)
    total_salidas = sum((_d(f["salidas"]) for f in filas), CERO)
    return {
        "resumen": {
            "saldo_inicial": str(saldo_inicial.quantize(q4)),
            "total_entradas": str(total_entradas.quantize(q4)),
            "total_salidas": str(total_salidas.quantize(q4)),
            "saldo_final": str((saldo_inicial + total_entradas - total_salidas).quantize(q4)),
            "existencia_actual": str(existencia_actual.quantize(q4)),
            "existencia_sin_movimiento": str(sin_mov_total.quantize(q4)),
            "costo_promedio": str(costo_promedio.quantize(Decimal("0.01"))),
            "total_registros": len(filas),
        },
        "results": filas,
    }
