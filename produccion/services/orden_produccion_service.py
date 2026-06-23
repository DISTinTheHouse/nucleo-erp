from django.db import transaction
from produccion.models import OrdenProduccion, OrdenProduccionDetalle
from produccion.utils.folios import generate_op_folio

class OrdenProduccionService:

    @staticmethod
    @transaction.atomic
    def save_orden_produccion(validated_data, user):
        op_rows = validated_data.pop('orden_produccion_detalle')
        folio_op = generate_op_folio(validated_data['empresa'], validated_data['sucursal'])
        op = OrdenProduccion.objects.create(folio_op=folio_op, usuario_asignado=user, **validated_data)
        bulk_data = []

        for row in op_rows:
            bulk_data.append(OrdenProduccionDetalle(op=op, **row))
        
        OrdenProduccionDetalle.objects.bulk_create(bulk_data)
        return op
    
    @staticmethod
    def get_formatted_op_detalle(op_id):
        op = OrdenProduccion.objects.filter(pk=op_id).prefetch_related(
            'orden_produccion_detalle__producto_variante__producto',
            'orden_produccion_detalle__producto_variante__talla',
            'orden_produccion_detalle__producto_variante__color',
            'orden_produccion_detalle__bom__materia_prima_detalle__componente',
            'orden_produccion_detalle__bom__materia_prima_detalle__unidad'
        ).first()

        if not op:
            return None

        products_data = {}

        for detail in op.orden_produccion_detalle.all():
            variant = detail.producto_variante
            if not variant:
                continue
            product = variant.producto
            if not product:
                continue

            product_id = product.id
            if product_id not in products_data:
                products_data[product_id] = {
                    "nombre": product.nombre,
                    "cantidades": {
                        "total": 0.0,
                        "tallas": {}
                    },
                    "habilitacion": {}
                }

            p_data = products_data[product_id]

            # 1. Cantidades
            qty = float(detail.cantidad)
            p_data["cantidades"]["total"] += qty
            talla_name = variant.talla.nombre if variant.talla else "N/A"
            color_name = variant.color.nombre if variant.color else "N/A"
            talla_color_key = (talla_name, color_name)
            p_data["cantidades"]["tallas"][talla_color_key] = p_data["cantidades"]["tallas"].get(talla_color_key, 0.0) + qty

            # 2. Habilitacion (materia prima)
            if detail.bom:
                for bom_det in detail.bom.materia_prima_detalle.all():
                    if not bom_det.activo or not bom_det.componente:
                        continue

                    comp = bom_det.componente
                    code = comp.cod_proscai if comp.cod_proscai else (comp.codigo if comp.codigo else "")
                    name = comp.nombre
                    unit = bom_det.unidad.clave.upper() if bom_det.unidad else ""

                    bom_qty = float(bom_det.cantidad)
                    needed = qty * bom_qty

                    key = (code, name, unit)
                    p_data["habilitacion"][key] = p_data["habilitacion"].get(key, 0.0) + needed

        productos_list = []
        for p_id, p_info in products_data.items():
            total_qty = p_info["cantidades"]["total"]
            total_qty_formatted = int(total_qty) if total_qty.is_integer() else round(total_qty, 2)

            tallas_list = []
            for (t_name, c_name), t_qty in p_info["cantidades"]["tallas"].items():
                t_qty_formatted = int(t_qty) if t_qty.is_integer() else round(t_qty, 2)
                tallas_list.append({
                    "talla": t_name,
                    "color": c_name,
                    "cantidad": t_qty_formatted
                })

            habilitacion_list = []
            for (code, name, unit), raw_qty in p_info["habilitacion"].items():
                raw_qty_formatted = int(raw_qty) if raw_qty.is_integer() else round(raw_qty, 4)

                habilitacion_list.append({
                    "codigo": code,
                    "descripcion": name,
                    "unidad": unit,
                    "total": raw_qty_formatted
                })

            productos_list.append({
                "nombre": p_info["nombre"],
                "cantidades": {
                    "total": total_qty_formatted,
                    "tallas": tallas_list
                },
                "habilitacion": habilitacion_list
            })

        from produccion.api.serializers import OrdenProduccionSerializer
        res_data = OrdenProduccionSerializer(op).data
        res_data.pop("orden_produccion_detalle", None)
        res_data["op_info"] = op.folio_op
        res_data["productos"] = productos_list
        return res_data