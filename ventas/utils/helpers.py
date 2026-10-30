from django.db.models import Q
from catalogo.compatibilidad import exigir_compatibilidad
from catalogo.models import CategoriaProductoTalla, Talla, Producto, ProductoVariante
from ventas.services.sku_produccion_service import nombre_es_muestra
from ventas.models import (
    CotizacionServicioExtra,
    CotizacionDetalle,
    CotizacionDetalleTalla,
    PedidoServicioExtra,
    PedidoDetalle,
    PedidoDetalleTalla,
)
from rest_framework.exceptions import ValidationError

# ``validar_tipos_servicio_array`` es agnóstico del framework y siempre lanza la
# ``ValidationError`` de Django, no la de DRF (ver ventas/servicios_bordado.py).
# Se importa con alias porque en este módulo ``ValidationError`` es la de DRF: sin
# distinguirlas, el ``except`` de abajo no atrapaba nada y un ``tipos_servicio``
# inválido salía como HTTP 500 en vez de 400.
from django.core.exceptions import ValidationError as DjangoValidationError

def _is_empty_json(value):
    return value in (None, "", [], {})


# --- Reflejante «Del inventario» (CO-06) ---------------------------------------
#
# La opción ``catalogo`` del reflejante (prenda del inventario + cinta) admite hasta
# 50 piezas por partida: la suma de las tallas de la partida que la llevan.
# ``reflejante_config`` es una lista de ``{opcion, posicion, tipo}``. Se revisa al
# guardar los renglones y, sobre lo guardado, al enviar a revisión, autorizar y
# aceptar cambios.
OPCION_REFLEJANTE_INVENTARIO = "catalogo"
MAX_PIEZAS_REFLEJANTE_INVENTARIO = 50


def _es_reflejante_inventario(lleva_reflejante, reflejante_config):
    if not lleva_reflejante:
        return False
    specs = [reflejante_config] if isinstance(reflejante_config, dict) else reflejante_config
    return isinstance(specs, list) and any(
        isinstance(s, dict)
        and str(s.get("opcion") or "").strip().lower() == OPCION_REFLEJANTE_INVENTARIO
        for s in specs
    )


def _exigir_maximo_reflejante_inventario(numero, piezas):
    if piezas > MAX_PIEZAS_REFLEJANTE_INVENTARIO:
        raise ValidationError(
            {
                "detalle": (
                    f"Partida {numero}: el reflejante «Del inventario» admite máximo "
                    f"{MAX_PIEZAS_REFLEJANTE_INVENTARIO} piezas y lleva {piezas}. "
                    "Cámbialo a «Fabricación especial» o reduce las piezas."
                )
            }
        )


def exigir_reflejante_inventario_guardado(detalles):
    """La misma regla sobre lo guardado. ``detalles``: los renglones de UN documento
    (``CotizacionDetalle`` o ``PedidoDetalle``); la partida N es el N-ésimo por id,
    el orden en que la API los devuelve. Una sola consulta (renglón x talla)."""
    piezas = {}
    for detalle_id, cantidad, lleva_reflejante, reflejante_config in detalles.order_by(
        "id"
    ).values_list("id", "tallas__cantidad", "tallas__lleva_reflejante", "tallas__reflejante_config"):
        piezas.setdefault(detalle_id, 0)
        if _es_reflejante_inventario(lleva_reflejante, reflejante_config):
            piezas[detalle_id] += cantidad or 0
    for numero, total in enumerate(piezas.values(), start=1):
        _exigir_maximo_reflejante_inventario(numero, total)


def exigir_color_si_hay_variantes(detalles):
    """Una línea de catálogo sin color cuyo producto tiene variantes deja sus tallas sin
    variante (sin SKU ni stock real). El borrador se guarda igual; esto se exige al
    enviar a revisión, autorizar y editar en mesa de control (#731). Una consulta."""
    sin_color = list(
        detalles.filter(producto__isnull=False, color__isnull=True, producto__variantes__activo=True)
        .filter(Q(producto_nombre_externo__isnull=True) | Q(producto_nombre_externo=""))
        .values_list("producto__nombre", flat=True).distinct()
    )
    if sin_color:
        raise ValidationError({"color": f"Elige el color de: {', '.join(sorted(sin_color))}."})


def _merge_detalle(rows):
    agrupado = {}
    for row in rows:
        # ``.get()``, no ``row["producto"]``: un renglón de MUESTRA llega sin
        # ``producto`` (o con ``None``) y el acceso por corchetes reventaba con
        # ``KeyError`` -> HTTP 500 en vez de un 400 legible.
        producto_id = row.get("producto")
        producto_nombre_externo = row.get("producto_nombre_externo")
        color_id = (
            row.get("color")
            if row.get("color") not in (None, "")
            else row.get("color_id")
        )
        if color_id in ("", 0):
            color_id = None
        categoria_producto_id = (
            row.get("categoria_producto")
            if row.get("categoria_producto") not in (None, "")
            else row.get("categoria_producto_id")
        )
        if categoria_producto_id in ("", 0):
            categoria_producto_id = None
        direccion_id = (
            row.get("direccion_envio_cliente")
            if row.get("direccion_envio_cliente") not in (None, "")
            else row.get("direccion_envio")
        )
        if direccion_id in ("", 0):
            direccion_id = None

        # Clave única por producto + nombre externo + color + dirección +
        # configuración de tallas.
        # Para que productos iguales con configuraciones distintas no se agrupen,
        # incluimos un hash de la configuración de las tallas en la key de agrupación.
        # ``producto_nombre_externo`` entra en la key porque en los renglones de
        # MUESTRA las otras cuatro componentes son idénticas para todos (producto,
        # color y dirección en ``None``, y ``tallas`` siempre ``[]``): sin él,
        # N renglones de muestra colapsaban en UNO solo, perdiendo texto en
        # silencio. Para un renglón de catálogo vale ``None``, así que la relación
        # de agrupación entre renglones de catálogo no cambia.
        import json

        tallas_raw = row.get("tallas") or []
        # Normalizamos tallas para que el orden no afecte, pero la config sí
        tallas_config_str = json.dumps(tallas_raw, sort_keys=True)

        key = (
            producto_id,
            producto_nombre_externo,
            color_id,
            categoria_producto_id,
            direccion_id,
            tallas_config_str,
        )
        entry = agrupado.get(key)
        if not entry:
            entry = {
                "producto": producto_id,
                "producto_nombre_externo": producto_nombre_externo,
                "color": color_id,
                "categoria_producto": categoria_producto_id,
                "direccion_envio_cliente": direccion_id,
                "precio_lista": row.get("precio_lista"),
                "precio_unitario": row.get("precio_unitario"),
                "costo_unitario": row.get("costo_unitario"),
                "tallas": [],
            }
            agrupado[key] = entry
        entry["tallas"] += tallas_raw

    for entry in agrupado.values():
        by_talla = {}
        for t in entry["tallas"]:
            talla_id = t["talla"]
            # Ahora, dentro de un mismo grupo (misma config base),
            # si hay varias entradas de la misma talla, las sumamos.
            agg = by_talla.get(talla_id)
            if not agg:
                by_talla[talla_id] = dict(t)
                continue
            agg["cantidad"] = int(agg["cantidad"]) + int(t["cantidad"])
        entry["tallas"] = list(by_talla.values())

    return list(agrupado.values())

def _ids_validos(valores):
    """``int`` de cada valor que lo admite; el resto se ignora (lo rechaza la validación)."""
    ids = set()
    for v in valores:
        if v in (None, "", 0):
            continue
        try:
            ids.add(int(v))
        except (TypeError, ValueError):
            pass
    return ids


def _save_cotizacion_detalle(cotizacion_obj, rows, empresa, user, validar_matriz=True):
    # ``validar_matriz``: al final se exige la matriz prenda/proceso (EC-575), dentro
    # de la transacción del llamador. Solo una copia del servidor la apaga.
    # Las líneas se recrean: el ``sku_muestra`` (lo escribe sólo
    # ``vincular-sku-muestra``) se arrastra por (nombre externo, color, talla)
    # para que no se pierda en cada edición.
    sku_muestra_previo = {
        (t.cotizacion_detalle.producto_nombre_externo, t.cotizacion_detalle.color_id, t.talla_id): t.sku_muestra
        for t in CotizacionDetalleTalla.objects.filter(
            cotizacion_detalle__cotizacion=cotizacion_obj,
            cotizacion_detalle__producto__isnull=True,
        ).exclude(sku_muestra__isnull=True).exclude(sku_muestra="").select_related("cotizacion_detalle")
    }
    # Misma razón con ``categoria_producto``: el pedido no la tiene, así que las
    # ediciones que parten de él (mesa de control) no la traen. Se conserva la de la
    # línea previa con la misma llave cuando la fila no menciona el campo.
    categoria_previa = {
        (d.producto_id, d.producto_nombre_externo, d.color_id): d.categoria_producto_id
        for d in CotizacionDetalle.objects.filter(
            cotizacion=cotizacion_obj, categoria_producto__isnull=False
        )
    }
    CotizacionDetalle.objects.filter(cotizacion=cotizacion_obj).delete()
    # Antes de agrupar: ``_merge_detalle`` rellena la clave con ``None`` y ya no se
    # distinguiría "no vino" de "vino vacía".
    rows = [
        r if "categoria_producto" in r or "categoria_producto_id" in r else {
            **r,
            "categoria_producto": categoria_previa.get((
                next(iter(_ids_validos([r.get("producto")])), None) if not r.get("producto_nombre_externo") else None,
                r.get("producto_nombre_externo") or None,
                next(iter(_ids_validos([r.get("color")])), None),
            )),
        }
        for r in rows
    ]
    rows = _merge_detalle(rows)

    from catalogo.models import CategoriaProducto as CategoriaProductoModel
    from catalogo.models import Color as ColorModel
    from terceros.models import DireccionCliente as DirModel

    # Los catálogos que referencian los renglones se resuelven UNA vez cada uno
    # (y no una consulta por renglón/talla): el costo del guardado ya no crece con
    # renglones × tallas. Las validaciones y sus mensajes son los de siempre, en
    # el mismo orden por renglón.
    es_superuser = getattr(user, "is_superuser", False)
    productos_qs = Producto.objects.filter(
        pk__in=_ids_validos(
            r.get("producto") for r in rows if not r.get("producto_nombre_externo")
        ),
        activo=True,
    )
    if not es_superuser and empresa:
        productos_qs = productos_qs.filter(empresa=empresa)
    productos = productos_qs.in_bulk()
    colores = ColorModel.objects.filter(
        pk__in=_ids_validos(r.get("color") for r in rows), activo=True
    ).in_bulk()
    # Siempre de la empresa de la cotización (también para superusuario, #426). Una
    # categoría desactivada solo se admite si la cotización ya la tenía (#429).
    categorias = CategoriaProductoModel.objects.filter(
        Q(activo=True) | Q(pk__in=set(categoria_previa.values())),
        pk__in=_ids_validos(
            r.get("categoria_producto") or r.get("categoria_producto_id") for r in rows
        ),
        empresa_id=cotizacion_obj.empresa_id,
    ).in_bulk()
    # Tallas permitidas por categoría, para las muestras (las de catálogo ya las
    # valida su variante). Una consulta; categoría sin tallas configuradas no restringe.
    tallas_por_categoria = {}
    for cat_id, talla_id in CategoriaProductoTalla.objects.filter(
        categoria_producto_id__in=categorias
    ).values_list("categoria_producto_id", "talla_id"):
        tallas_por_categoria.setdefault(cat_id, set()).add(talla_id)
    direcciones = DirModel.objects.filter(
        pk__in=_ids_validos(r.get("direccion_envio_cliente") for r in rows),
        activo=True,
        empresa_id=cotizacion_obj.empresa_id,
        cliente_id=getattr(cotizacion_obj, "cliente_id", None),
    ).in_bulk()
    tallas_catalogo = Talla.objects.filter(
        pk__in=_ids_validos(
            t.get("talla") for r in rows for t in (r.get("tallas") or [])
        ),
        activo=True,
    ).in_bulk()
    variantes = {}
    if productos:
        # ``.first()`` de antes (orden por pk): la primera variante de cada llave gana.
        for v in ProductoVariante.objects.filter(
            producto__in=productos, empresa_id=cotizacion_obj.empresa_id
        ).order_by("pk"):
            variantes.setdefault((v.producto_id, v.color_id, v.talla_id), v)

    detalles = []
    tallas_por_detalle = []
    for numero, item in enumerate(rows, start=1):
        producto = None
        producto_nombre_externo = item.get("producto_nombre_externo")
        producto_id = item.get("producto")

        if not producto_nombre_externo and producto_id not in (None, "", 0):
            try:
                producto_id = int(producto_id)
            except Exception:
                producto_id = None
            if producto_id:
                producto = productos.get(producto_id)
                if not producto:
                    raise ValidationError({"detalle": f"Producto inválido: {producto_id}"})

        if producto is None and not producto_nombre_externo:
            raise ValidationError({"detalle": "Debe indicar un producto del catálogo o un producto_nombre_externo."})

        color_obj = None
        color_id = item.get("color")
        if color_id not in (None, "", 0):
            try:
                color_obj = colores.get(int(color_id))
            except Exception:
                color_obj = None
            if not color_obj:
                raise ValidationError({"detalle": f"Color inválido: {color_id}"})

        categoria_producto_obj = None
        categoria_producto_id = item.get("categoria_producto") or item.get("categoria_producto_id")
        if categoria_producto_id not in (None, "", 0):
            try:
                categoria_producto_obj = categorias.get(int(categoria_producto_id))
            except Exception:
                categoria_producto_obj = None
            if not categoria_producto_obj:
                raise ValidationError(
                    {"detalle": f"Categoría de producto inválida: {categoria_producto_id}"}
                )

        direccion_obj = None
        direccion_id = item.get("direccion_envio_cliente")
        if direccion_id not in (None, "", 0):
            try:
                direccion_obj = direcciones.get(int(direccion_id))
            except Exception:
                direccion_obj = None
            if not direccion_obj:
                raise ValidationError(
                    {"detalle": f"Dirección de envío inválida: {direccion_id}"}
                )

        precio_unitario = item.get("precio_unitario")
        if precio_unitario is None:
            precio_unitario = getattr(producto, "precio_base", None) or 0

        precio_lista = item.get("precio_lista")
        if precio_lista is None:
            precio_lista = getattr(producto, "precio_base", None) or 0

        detalles.append(
            CotizacionDetalle(
                cotizacion=cotizacion_obj,
                producto=producto,
                producto_nombre_externo=producto_nombre_externo,
                color=color_obj,
                categoria_producto=categoria_producto_obj,
                direccion_envio_cliente=direccion_obj,
                precio_lista=precio_lista,
                precio_unitario=precio_unitario,
                costo_unitario=item.get("costo_unitario"),
                subtotal_linea=0,
            )
        )
        tallas_nuevas = []
        tallas_por_detalle.append(tallas_nuevas)
        piezas_inventario = 0
        for t in item.get("tallas") or []:
            try:
                talla = tallas_catalogo.get(int(t["talla"]))
            except (TypeError, ValueError):
                talla = None
            if not talla:
                raise ValidationError({"detalle": f"Talla inválida: {t['talla']}"})
            permitidas = tallas_por_categoria.get(getattr(categoria_producto_obj, "pk", None))
            # La copia del servidor desde mesa de control (``validar_matriz=False``)
            # no revalida: el pedido ya aceptó la talla y no se puede corregir ahí (#761).
            if validar_matriz and producto is None and permitidas and talla.pk not in permitidas:
                raise ValidationError({"detalle": (
                    f"Partida {numero}: la talla {talla.nombre} no corresponde a la categoría "
                    f"{categoria_producto_obj.nombre}."
                )})
            if t.get("lleva_bordado") and t.get("bordado_config") is None:
                raise ValidationError(
                    {
                        "detalle": "Falta bordado_config en una talla marcada con lleva_bordado=true."
                    }
                )
            bruto_cfg = t.get("bordado_config") or {}
            if isinstance(bruto_cfg, dict) and "tipos_servicio" in bruto_cfg:
                from ventas.servicios_bordado import validar_tipos_servicio_array
                try:
                    bruto_cfg["tipos_servicio"] = validar_tipos_servicio_array(
                        bruto_cfg["tipos_servicio"],
                        campo_label="bordado_config.tipos_servicio",
                    )
                except DjangoValidationError as e:
                    raise ValidationError({"detalle": "; ".join(e.messages)})
            lleva_reflejante = bool(
                t.get("lleva_reflejante") or t.get("lleva_serigrafia")
            )
            reflejante_config = t.get("reflejante_config")
            if _is_empty_json(reflejante_config):
                reflejante_config = t.get("serigrafia_config")
            if lleva_reflejante and _is_empty_json(reflejante_config):
                raise ValidationError(
                    {
                        "detalle": "Falta reflejante_config en una talla marcada con lleva_reflejante=true."
                    }
                )
            if t.get("lleva_corte_manga") and _is_empty_json(
                t.get("corte_manga_config")
            ):
                raise ValidationError(
                    {
                        "detalle": "Falta corte_manga_config en una talla marcada con lleva_corte_manga=true."
                    }
                )
            if t.get("lleva_cambio_talla") and _is_empty_json(
                t.get("cambio_talla_config")
            ):
                raise ValidationError(
                    {
                        "detalle": "Falta cambio_talla_config en una talla marcada con lleva_cambio_talla=true."
                    }
                )
            if _es_reflejante_inventario(lleva_reflejante, reflejante_config):
                piezas_inventario += int(t["cantidad"])

            variante_obj = None
            if producto:
                variante_obj = variantes.get(
                    (producto.pk, getattr(color_obj, "pk", None), talla.pk)
                )

            # Sólo hay SKU con producto de catálogo: una línea de muestra no
            # tiene (``None``) y el de producción vive en ``sku_muestra``. Con
            # variante manda ``variante.sku``; sin ella, el armado por códigos.
            sku_snapshot = None
            if producto:
                sku_snapshot = getattr(variante_obj, "sku", None)
                if not sku_snapshot:
                    codigo_producto = (getattr(producto, "codigo", None) or "").strip()
                    codigo_color = (getattr(color_obj, "codigo", None) or "").strip()
                    codigo_talla = (getattr(talla, "nombre", None) or "").strip()
                    sku_snapshot = f"{codigo_producto}{codigo_color}{codigo_talla}".replace(
                        " ", ""
                    ).upper()

            tallas_nuevas.append(
                CotizacionDetalleTalla(
                    talla=talla,
                    cantidad=t["cantidad"],
                    precio_unitario=precio_unitario,
                    subtotal_talla=0,
                    lleva_bordado=bool(t.get("lleva_bordado")),
                    bordado_config=t.get("bordado_config"),
                    lleva_reflejante=lleva_reflejante,
                    reflejante_config=reflejante_config,
                    lleva_corte_manga=bool(t.get("lleva_corte_manga")),
                    corte_manga_config=t.get("corte_manga_config"),
                    lleva_cambio_talla=bool(t.get("lleva_cambio_talla")),
                    cambio_talla_config=t.get("cambio_talla_config"),
                    sku=sku_snapshot or None,
                    sku_muestra=sku_muestra_previo.get(
                        (producto_nombre_externo, getattr(color_obj, "pk", None), talla.pk)
                    ) if not producto else None,
                    requiere_produccion=nombre_es_muestra(producto_nombre_externo),
                    variante=variante_obj,
                )
            )
        _exigir_maximo_reflejante_inventario(numero, piezas_inventario)

    # Dos INSERT en lote (renglones, luego tallas) en vez de uno por fila.
    CotizacionDetalle.objects.bulk_create(detalles)
    todas = []
    for detalle, tallas_nuevas in zip(detalles, tallas_por_detalle):
        for t in tallas_nuevas:
            t.cotizacion_detalle = detalle
            todas.append(t)
    CotizacionDetalleTalla.objects.bulk_create(todas)
    if validar_matriz:
        exigir_compatibilidad(
            cotizacion_obj.empresa_id,
            CotizacionDetalleTalla.objects.filter(cotizacion_detalle__cotizacion=cotizacion_obj),
        )

def _save_servicios_extras(cotizacion_obj, rows):
    CotizacionServicioExtra.objects.filter(cotizacion=cotizacion_obj).delete()
    CotizacionServicioExtra.objects.bulk_create(
        [
            CotizacionServicioExtra(
                cotizacion=cotizacion_obj,
                nombre=row.get("nombre") or "",
                monto=row.get("monto") or 0,
                cantidad=row.get("cantidad") or 1,
                visible_en_factura=bool(row.get("visible_en_factura", True)),
            )
            for row in rows or []
        ]
    )


def _save_pedido_detalle(pedido_obj, rows, empresa, user):
    existing_detalles = {
        detalle.pk: detalle
        for detalle in PedidoDetalle.objects.filter(pedido=pedido_obj).prefetch_related("tallas")
    }
    touched_ids = set()
    for numero, item in enumerate(rows or [], start=1):
        producto = None
        producto_nombre_externo = item.get("producto_nombre_externo")
        producto_id = item.get("producto")
        detalle_id = item.get("id")

        if not producto_nombre_externo and producto_id not in (None, "", 0):
            try:
                producto_id = int(producto_id)
            except Exception:
                producto_id = None
            if producto_id:
                producto = Producto.objects.filter(pk=producto_id, activo=True).first()
                if not getattr(user, "is_superuser", False) and empresa:
                    producto = Producto.objects.filter(
                        pk=producto_id, empresa=empresa, activo=True
                    ).first()
                if not producto:
                    raise ValidationError({"detalle": f"Producto inválido: {producto_id}"})

        if producto is None and not producto_nombre_externo:
            raise ValidationError(
                {
                    "detalle": "Debe indicar un producto del catálogo o un producto_nombre_externo."
                }
            )

        color_obj = None
        color_id = item.get("color")
        if color_id not in (None, "", 0):
            try:
                from catalogo.models import Color as ColorModel

                color_obj = ColorModel.objects.filter(
                    pk=int(color_id), activo=True
                ).first()
            except Exception:
                color_obj = None
            if not color_obj:
                raise ValidationError({"detalle": f"Color inválido: {color_id}"})

        direccion_obj = None
        direccion_id = item.get("direccion_envio_cliente")
        if direccion_id not in (None, "", 0):
            try:
                from terceros.models import DireccionCliente as DirModel

                direccion_obj = DirModel.objects.filter(
                    pk=int(direccion_id),
                    activo=True,
                    empresa=pedido_obj.empresa,
                    cliente_id=getattr(pedido_obj, "cliente_id", None),
                ).first()
            except Exception:
                direccion_obj = None
            if not direccion_obj:
                raise ValidationError(
                    {"detalle": f"Dirección de envío inválida: {direccion_id}"}
                )

        precio_unitario = item.get("precio_unitario")
        if precio_unitario is None:
            precio_unitario = getattr(producto, "precio_base", None) or 0

        precio_lista = item.get("precio_lista")
        if precio_lista is None:
            precio_lista = getattr(producto, "precio_base", None) or 0

        pedido_det = None
        if detalle_id not in (None, "", 0):
            try:
                detalle_id = int(detalle_id)
            except Exception:
                detalle_id = None
            pedido_det = existing_detalles.get(detalle_id)
            if pedido_det is None:
                raise ValidationError(
                    {"detalle": f"El renglón de pedido no existe: {item.get('id')}"}
                )
        else:
            pedido_det = PedidoDetalle(pedido=pedido_obj)

        pedido_det.producto = producto
        pedido_det.producto_nombre_externo = producto_nombre_externo
        pedido_det.color = color_obj
        pedido_det.direccion_envio_cliente = direccion_obj
        pedido_det.precio_lista = precio_lista
        pedido_det.precio_unitario = precio_unitario
        pedido_det.costo_unitario = item.get("costo_unitario")
        pedido_det.subtotal_linea = 0
        pedido_det.save()
        touched_ids.add(pedido_det.pk)

        existing_tallas = {
            talla.pedido_detalle_id: []
            for talla in PedidoDetalleTalla.objects.filter(pedido_detalle=pedido_det)
        }
        tallas_actuales = list(
            PedidoDetalleTalla.objects.filter(pedido_detalle=pedido_det).select_related("talla")
        )
        for talla_row in tallas_actuales:
            existing_tallas.setdefault(talla_row.pedido_detalle_id, []).append(talla_row)
        tallas_usadas = set()
        piezas_inventario = 0

        for t in item.get("tallas") or []:
            talla = Talla.objects.filter(pk=t["talla"], activo=True).first()
            if not talla:
                raise ValidationError({"detalle": f"Talla inválida: {t['talla']}"})
            if t.get("lleva_bordado") and t.get("bordado_config") is None:
                raise ValidationError(
                    {
                        "detalle": "Falta bordado_config en una talla marcada con lleva_bordado=true."
                    }
                )
            bruto_cfg = t.get("bordado_config") or {}
            if isinstance(bruto_cfg, dict) and "tipos_servicio" in bruto_cfg:
                from ventas.servicios_bordado import validar_tipos_servicio_array

                try:
                    bruto_cfg["tipos_servicio"] = validar_tipos_servicio_array(
                        bruto_cfg["tipos_servicio"],
                        campo_label="bordado_config.tipos_servicio",
                    )
                except DjangoValidationError as e:
                    raise ValidationError({"detalle": "; ".join(e.messages)})
            lleva_reflejante = bool(
                t.get("lleva_reflejante") or t.get("lleva_serigrafia")
            )
            reflejante_config = t.get("reflejante_config")
            if _is_empty_json(reflejante_config):
                reflejante_config = t.get("serigrafia_config")
            if lleva_reflejante and _is_empty_json(reflejante_config):
                raise ValidationError(
                    {
                        "detalle": "Falta reflejante_config en una talla marcada con lleva_reflejante=true."
                    }
                )
            if t.get("lleva_corte_manga") and _is_empty_json(
                t.get("corte_manga_config")
            ):
                raise ValidationError(
                    {
                        "detalle": "Falta corte_manga_config en una talla marcada con lleva_corte_manga=true."
                    }
                )
            if t.get("lleva_cambio_talla") and _is_empty_json(
                t.get("cambio_talla_config")
            ):
                raise ValidationError(
                    {
                        "detalle": "Falta cambio_talla_config en una talla marcada con lleva_cambio_talla=true."
                    }
                )
            if _es_reflejante_inventario(lleva_reflejante, reflejante_config):
                piezas_inventario += int(t["cantidad"])

            variante_obj = None
            if producto:
                variante_obj = ProductoVariante.objects.filter(
                    producto=producto,
                    color=color_obj,
                    talla=talla,
                    empresa=pedido_obj.empresa,
                ).first()

            talla_existente = next(
                (
                    row
                    for row in tallas_actuales
                    if row.talla_id == talla.pk and row.pk not in tallas_usadas
                ),
                None,
            )
            if talla_existente is None:
                talla_existente = PedidoDetalleTalla(pedido_detalle=pedido_det, talla=talla)

            talla_existente.cantidad = t["cantidad"]
            talla_existente.precio_unitario = precio_unitario
            talla_existente.subtotal_talla = 0
            talla_existente.lleva_bordado = bool(t.get("lleva_bordado"))
            talla_existente.bordado_config = t.get("bordado_config")
            talla_existente.lleva_reflejante = lleva_reflejante
            talla_existente.reflejante_config = reflejante_config
            talla_existente.lleva_corte_manga = bool(t.get("lleva_corte_manga"))
            talla_existente.corte_manga_config = t.get("corte_manga_config")
            talla_existente.lleva_cambio_talla = bool(t.get("lleva_cambio_talla"))
            talla_existente.cambio_talla_config = t.get("cambio_talla_config")
            talla_existente.requiere_produccion = nombre_es_muestra(producto_nombre_externo)
            talla_existente.variante = variante_obj
            if producto:
                # La línea ya es de catálogo: el SKU de muestra no aplica.
                talla_existente.sku_muestra = None
            talla_existente.save()
            tallas_usadas.add(talla_existente.pk)
        _exigir_maximo_reflejante_inventario(numero, piezas_inventario)

        sobrantes = [row for row in tallas_actuales if row.pk not in tallas_usadas]
        if sobrantes:
            # Mesa de control puede eliminar tallas desde edición estricta
            # porque ya validamos en _get_bloqueos_edicion_estricta() que no hay documentos ligados
            sobrantes_ids = [row.pk for row in sobrantes]
            PedidoDetalleTalla.objects.filter(pk__in=sobrantes_ids).delete()

    sobrantes_detalle = [
        detalle_id for detalle_id in existing_detalles.keys() if detalle_id not in touched_ids
    ]
    if sobrantes_detalle:
        # Mesa de control puede eliminar renglones desde edición estricta
        # porque ya validamos en _get_bloqueos_edicion_estricta() que no hay documentos ligados
        PedidoDetalle.objects.filter(pk__in=sobrantes_detalle).delete()
    # Matriz prenda/proceso (EC-575), dentro de la transacción del llamador.
    exigir_compatibilidad(
        pedido_obj.empresa_id, PedidoDetalleTalla.objects.filter(pedido_detalle__pedido=pedido_obj)
    )


def _save_pedido_servicios_extras(pedido_obj, rows):
    existentes = list(PedidoServicioExtra.objects.filter(pedido=pedido_obj).order_by("id"))
    rows = rows or []
    for index, row in enumerate(rows):
        extra = existentes[index] if index < len(existentes) else PedidoServicioExtra(pedido=pedido_obj)
        extra.nombre = row.get("nombre") or ""
        extra.monto = row.get("monto") or 0
        extra.cantidad = row.get("cantidad") or 1
        extra.visible_en_factura = bool(row.get("visible_en_factura", True))
        extra.save()
    if len(existentes) > len(rows):
        # Mesa de control puede eliminar servicios extras desde edición estricta
        # Eliminar los que no vienen en el payload actual
        ids_a_eliminar = [existentes[i].pk for i in range(len(rows), len(existentes))]
        PedidoServicioExtra.objects.filter(pk__in=ids_a_eliminar).delete()


def nombre_para_mostrar(first_name, last_name, email):
    """Nombre de un usuario para mostrar: ``get_full_name().strip() or email``.

    Recibe las partes y no el usuario para servir también a un listado que solo
    anota esas columnas (``PedidoListSerializer``). ``Usuario`` no redefine
    ``get_full_name``: es el ``"first last".strip()`` de ``AbstractUser``.
    """
    return f"{first_name or ''} {last_name or ''}".strip() or email
