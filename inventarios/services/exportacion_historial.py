"""PDF/XLSX del historial por SKU y de la valuación al cierre.

Mismo formato y helpers que los reportes por periodo (``reportes_exportacion``).
"""
from django.utils import timezone
from django.utils.text import slugify

from inventarios.api.reportes_periodo import to_decimal
from inventarios.services.reportes_exportacion import (
    _FMT_CANTIDAD,
    _FMT_DINERO,
    _FMT_ENTERO,
    _FMT_FECHA_HORA,
    _SIN_DATO,
    TIPOS_MOVIMIENTO_ETIQUETA,
    Exportacion,
    _fecha_hora_excel,
    _local,
    _pdf,
    _texto,
    _xlsx,
    formato_cantidad,
    formato_dinero,
    formato_fecha,
    formato_fecha_corta,
    formato_generado,
)

_COLUMNAS_HISTORIAL_PDF = (
    ("Fecha", 1.5, "LEFT", True, False),
    ("Tipo", 1.1, "LEFT", False, True),
    ("Origen", 1.9, "LEFT", False, True),
    ("Almacén", 2.0, "LEFT", False, True),
    ("Ubicación", 1.3, "LEFT", False, True),
    ("Entrada", 1.0, "RIGHT", False, False),
    ("Salida", 1.0, "RIGHT", False, False),
    ("Saldo", 1.1, "RIGHT", True, False),
    ("Saldo $", 1.4, "RIGHT", False, False),
    ("Usuario", 1.8, "LEFT", False, True),
    ("Notas", 2.4, "LEFT", False, True),
)
_COLUMNAS_HISTORIAL_XLSX = (
    ("Fecha", 17, _FMT_FECHA_HORA),
    ("Tipo", 14, None),
    ("Origen", 14, None),
    ("Folio", 18, None),
    ("Almacén", 24, None),
    ("Ubicación", 14, None),
    ("Entrada", 12, _FMT_CANTIDAD),
    ("Salida", 12, _FMT_CANTIDAD),
    ("Costo Unitario", 14, _FMT_DINERO),
    ("Saldo", 12, _FMT_CANTIDAD),
    ("Saldo $", 15, _FMT_DINERO),
    ("Usuario", 26, None),
    ("Notas", 40, None),
)


def _rango_texto(inicio, final):
    if not inicio:
        return "Toda la historia"
    return f"{formato_fecha(inicio)} — {formato_fecha(final)}"


def exportar_historial_sku(reporte, formato, almacen_nombre=None, generado_en=None):
    generado_en = generado_en or timezone.now()
    p = reporte["producto"]
    filas = reporte["results"]
    ini, fin = reporte["saldo_inicial"], reporte["saldo_final"]
    almacen = almacen_nombre or (
        "Todos los almacenes" if not reporte["almacen_id"] else f"Almacén #{reporte['almacen_id']}"
    )
    descripcion = " · ".join(str(x) for x in (p["descripcion"], p["color"], p["talla"]) if x)
    filtros = [
        ("SKU", p["sku"]),
        ("Artículo", descripcion or _SIN_DATO),
        ("Almacén", almacen),
        ("Periodo", _rango_texto(reporte["fecha_inicio"], reporte["fecha_final"])),
    ]
    rango = (
        f"{reporte['fecha_inicio'].isoformat()}-a-{reporte['fecha_final'].isoformat()}"
        if reporte["fecha_inicio"] else "completo"
    )
    nombre = f"historial-sku-{slugify(p['sku'])}-{rango}.{formato}"
    titulo = "Historial de SKU"
    subtitulo = f"{p['sku']} · {almacen}"
    if formato == "pdf":
        contenido = _pdf(
            titulo_documento=f"{titulo} — {subtitulo}",
            titulo=titulo,
            subtitulo=subtitulo,
            filtros=filtros,
            generacion=[
                ("Costo promedio actual", formato_dinero(p["costo_promedio_actual"])),
                ("Registros", f"{len(filas):,}"),
                ("Generado el", formato_generado(generado_en)),
            ],
            resumen=[
                ("SALDO INICIAL", f"{formato_cantidad(ini['piezas'])} / {formato_dinero(ini['valor'])}"),
                ("ENTRADAS", formato_cantidad(reporte["totales"]["entradas"])),
                ("SALIDAS", formato_cantidad(reporte["totales"]["salidas"])),
                ("SALDO FINAL", f"{formato_cantidad(fin['piezas'])} / {formato_dinero(fin['valor'])}"),
            ],
            seccion="MOVIMIENTOS",
            columnas=_COLUMNAS_HISTORIAL_PDF,
            filas=[
                [
                    (
                        f"{formato_fecha_corta(f['fecha'])}\n{_local(f['fecha']).strftime('%H:%M')}"
                        if f["fecha"] else _SIN_DATO
                    ),
                    TIPOS_MOVIMIENTO_ETIQUETA.get(f["tipo_movimiento"], _texto(f["tipo_movimiento"])),
                    f"{f['origen_tipo']}\n{_texto(f['origen_folio'])}",
                    _texto(f["almacen_nombre"]),
                    _texto(f["ubicacion_codigo"]),
                    formato_cantidad(f["entrada"]),
                    formato_cantidad(f["salida"]),
                    formato_cantidad(f["saldo"]),
                    formato_dinero(f["saldo_valor"]),
                    _texto(f["usuario"]),
                    _texto(f["notas"]),
                ]
                for f in filas
            ],
            vacio="Sin movimientos en el periodo.",
            pie=f"Historial de SKU — {subtitulo}",
        )
    else:
        contenido = _xlsx(
            titulo_hoja="Historial",
            titulo=titulo,
            subtitulo=subtitulo,
            filtros=filtros,
            generacion=[
                ("Costo promedio actual", to_decimal(p["costo_promedio_actual"]), _FMT_DINERO),
                ("Registros", len(filas), _FMT_ENTERO),
                ("Generado el", formato_generado(generado_en), None),
            ],
            resumen=[
                ("Saldo inicial (piezas)", to_decimal(ini["piezas"]), _FMT_CANTIDAD),
                ("Saldo inicial ($)", to_decimal(ini["valor"]), _FMT_DINERO),
                ("Entradas", to_decimal(reporte["totales"]["entradas"]), _FMT_CANTIDAD),
                ("Salidas", to_decimal(reporte["totales"]["salidas"]), _FMT_CANTIDAD),
                ("Saldo final (piezas)", to_decimal(fin["piezas"]), _FMT_CANTIDAD),
                ("Saldo final ($)", to_decimal(fin["valor"]), _FMT_DINERO),
            ],
            seccion="MOVIMIENTOS",
            columnas=_COLUMNAS_HISTORIAL_XLSX,
            filas=[
                [
                    _fecha_hora_excel(f["fecha"]),
                    TIPOS_MOVIMIENTO_ETIQUETA.get(f["tipo_movimiento"], f["tipo_movimiento"]),
                    f["origen_tipo"],
                    f["origen_folio"],
                    f["almacen_nombre"],
                    f["ubicacion_codigo"],
                    to_decimal(f["entrada"]),
                    to_decimal(f["salida"]),
                    to_decimal(f["costo_unitario"]),
                    to_decimal(f["saldo"]),
                    to_decimal(f["saldo_valor"]),
                    f["usuario"],
                    f["notas"],
                ]
                for f in filas
            ],
        )
    return Exportacion(contenido, formato, nombre)


_COLUMNAS_VALUACION_PDF = (
    ("Almacén", 2.2, "LEFT", False, True),
    ("SKU", 1.6, "LEFT", True, True),
    ("Producto", 3.6, "LEFT", False, True),
    ("Color", 1.3, "LEFT", False, True),
    ("Talla", 0.8, "LEFT", False, True),
    ("Existencia", 1.3, "RIGHT", True, False),
    ("Costo Unitario", 1.4, "RIGHT", False, False),
    ("Costo Total", 1.6, "RIGHT", True, False),
)
_COLUMNAS_VALUACION_XLSX = (
    ("Almacén", 26, None),
    ("SKU", 18, None),
    ("Producto", 40, None),
    ("Color", 16, None),
    ("Talla", 10, None),
    ("Existencia", 14, _FMT_CANTIDAD),
    ("Costo Unitario", 15, _FMT_DINERO),
    ("Costo Total", 16, _FMT_DINERO),
)


def exportar_valuacion(reporte, formato, generado_en=None):
    generado_en = generado_en or timezone.now()
    almacen_id = reporte["almacen_id"]
    alm = reporte["almacenes"].get(almacen_id) or {}
    almacen = alm.get("nombre") or ("Todos los almacenes" if not almacen_id else f"Almacén #{almacen_id}")
    filas = reporte["results"]
    resumen = reporte["resumen"]
    filtros = [("Almacén", almacen), ("Fecha de corte", formato_fecha(reporte["fecha"]))]
    codigo = alm.get("codigo") or ("todos" if not almacen_id else f"almacen-{almacen_id}")
    nombre = f"valuacion-inventario-{slugify(codigo)}-{reporte['fecha'].isoformat()}.{formato}"
    titulo = "Valuación de Inventario al Cierre"
    subtitulo = f"{almacen} · al {formato_fecha(reporte['fecha'])}"
    if formato == "pdf":
        contenido = _pdf(
            titulo_documento=f"{titulo} — {subtitulo}",
            titulo=titulo,
            subtitulo=subtitulo,
            filtros=filtros,
            generacion=[("Registros", f"{len(filas):,}"), ("Generado el", formato_generado(generado_en))],
            resumen=[
                ("ARTÍCULOS", f"{resumen['articulos']:,}"),
                ("PIEZAS", formato_cantidad(resumen["piezas"])),
                ("VALOR TOTAL", formato_dinero(resumen["valor"])),
            ],
            seccion="EXISTENCIAS VALUADAS",
            columnas=_COLUMNAS_VALUACION_PDF,
            filas=[
                [
                    _texto(f["almacen_nombre"]),
                    _texto(f["sku"]),
                    _texto(f["producto_nombre"]),
                    _texto(f["color"]),
                    _texto(f["talla"]),
                    formato_cantidad(f["existencia"]),
                    formato_dinero(f["costo_unitario"]),
                    formato_dinero(f["costo_total"]),
                ]
                for f in filas
            ],
            vacio="Sin existencias a la fecha de corte.",
            pie=f"Valuación de Inventario — {subtitulo}",
        )
    else:
        contenido = _xlsx(
            titulo_hoja="Valuacion",
            titulo=titulo,
            subtitulo=subtitulo,
            filtros=filtros,
            generacion=[("Registros", len(filas), _FMT_ENTERO), ("Generado el", formato_generado(generado_en), None)],
            resumen=[
                ("Artículos", resumen["articulos"], _FMT_ENTERO),
                ("Piezas", to_decimal(resumen["piezas"]), _FMT_CANTIDAD),
                ("Valor total", to_decimal(resumen["valor"]), _FMT_DINERO),
            ],
            seccion="EXISTENCIAS VALUADAS",
            columnas=_COLUMNAS_VALUACION_XLSX,
            filas=[
                [
                    f["almacen_nombre"],
                    f["sku"],
                    f["producto_nombre"],
                    f["color"],
                    f["talla"],
                    to_decimal(f["existencia"]),
                    to_decimal(f["costo_unitario"]),
                    to_decimal(f["costo_total"]),
                ]
                for f in filas
            ],
        )
    return Exportacion(contenido, formato, nombre)
