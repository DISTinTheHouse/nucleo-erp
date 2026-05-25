from catalogo.models import Talla, Producto
from ventas.models import CotizacionServicioExtra, CotizacionDetalle, CotizacionDetalleTalla
from rest_framework.exceptions import ValidationError

def _is_empty_json(value):
    return value in (None, "", [], {})

def _merge_detalle(rows):
    agrupado = {}
    for row in rows:
        producto_id = row["producto"]
        color_id = (
            row.get("color")
            if row.get("color") not in (None, "")
            else row.get("color_id")
        )
        if color_id in ("", 0):
            color_id = None
        direccion_id = (
            row.get("direccion_envio_cliente")
            if row.get("direccion_envio_cliente") not in (None, "")
            else row.get("direccion_envio")
        )
        if direccion_id in ("", 0):
            direccion_id = None

        # Clave única por producto + color + dirección + configuración de tallas
        # Para que productos iguales con configuraciones distintas no se agrupen,
        # incluimos un hash de la configuración de las tallas en la key de agrupación.
        import json

        tallas_raw = row.get("tallas") or []
        # Normalizamos tallas para que el orden no afecte, pero la config sí
        tallas_config_str = json.dumps(tallas_raw, sort_keys=True)

        key = (producto_id, color_id, direccion_id, tallas_config_str)
        entry = agrupado.get(key)
        if not entry:
            entry = {
                "producto": producto_id,
                "color": color_id,
                "direccion_envio_cliente": direccion_id,
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

def _save_cotizacion_detalle(cotizacion_obj, rows, empresa, user):
    CotizacionDetalle.objects.filter(cotizacion=cotizacion_obj).delete()
    rows = _merge_detalle(rows)
    for item in rows:
        producto = Producto.objects.filter(pk=item["producto"], activo=True).first()
        if not getattr(user, "is_superuser", False) and empresa:
            producto = Producto.objects.filter(
                pk=item["producto"], empresa=empresa, activo=True
            ).first()
        if not producto:
            raise ValidationError({"detalle": f"Producto inválido: {item['producto']}"})

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
                    empresa=cotizacion_obj.empresa,
                    cliente_id=getattr(cotizacion_obj, "cliente_id", None),
                ).first()
            except Exception:
                direccion_obj = None
            if not direccion_obj:
                raise ValidationError(
                    {"detalle": f"Dirección de envío inválida: {direccion_id}"}
                )

        precio_unitario = item.get("precio_unitario")
        if precio_unitario is None:
            precio_unitario = producto.precio_base or 0

        cot_det = CotizacionDetalle.objects.create(
            cotizacion=cotizacion_obj,
            producto=producto,
            color=color_obj,
            direccion_envio_cliente=direccion_obj,
            precio_lista=producto.precio_base or 0,
            precio_unitario=precio_unitario,
            costo_unitario=item.get("costo_unitario"),
            subtotal_linea=0,
        )
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

            # Calcular SKU snapshot
            codigo_producto = (getattr(producto, "codigo", None) or "").strip()
            codigo_color = (getattr(color_obj, "codigo", None) or "").strip()
            codigo_talla = (getattr(talla, "nombre", None) or "").strip()
            sku_snapshot = f"{codigo_producto}{codigo_color}{codigo_talla}".replace(
                " ", ""
            ).upper()

            CotizacionDetalleTalla.objects.create(
                cotizacion_detalle=cot_det,
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
            )

def _save_servicios_extras(cotizacion_obj, rows):
    CotizacionServicioExtra.objects.filter(cotizacion=cotizacion_obj).delete()
    for row in rows or []:
        CotizacionServicioExtra.objects.create(
            cotizacion=cotizacion_obj,
            nombre=row.get("nombre") or "",
            monto=row.get("monto") or 0,
            cantidad=row.get("cantidad") or 1,
            visible_en_factura=bool(row.get("visible_en_factura", True)),
        )