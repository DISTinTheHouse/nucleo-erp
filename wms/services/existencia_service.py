from collections import defaultdict
from decimal import Decimal

from django.db import models
from django.db.models import Sum

from inventarios.models import Existencia, inventario_reservas


class ExistenciaService:
    @staticmethod
    def _normalize(value):
        return Decimal(str(value or "0"))

    @staticmethod
    def get_existencia(almacen, producto, producto_variante):
        filters = {
            "almacen": almacen,
        }

        if producto_variante:
            filters["producto_variante"] = producto_variante
        else:
            filters["producto"] = producto

        return Existencia.objects.select_for_update().filter(**filters).first()

    @classmethod
    def _sum_existencia(cls, almacen_id, producto_id, producto_variante_id):
        filters = {"almacen_id": almacen_id}
        if producto_variante_id:
            filters["producto_variante_id"] = producto_variante_id
        else:
            filters["producto_id"] = producto_id
        return cls._normalize(
            Existencia.objects.filter(**filters).aggregate(total=Sum("cantidad"))["total"] or "0"
        )

    @classmethod
    def _sum_reservas_activas(cls, almacen_id, pedido_detalle_talla_ids, keys_by_talla):
        if not pedido_detalle_talla_ids:
            return defaultdict(lambda: Decimal("0"))

        rows = (
            inventario_reservas.objects.filter(
                estado=inventario_reservas.Estado.ACTIVA,
                almacen_id=almacen_id,
                pedido_detalle_talla_id__in=list(pedido_detalle_talla_ids),
            )
            .values("pedido_detalle_talla_id")
            .annotate(total=Sum("cantidad"))
        )
        reservada_map = defaultdict(lambda: Decimal("0"))
        for row in rows:
            talla_id = row["pedido_detalle_talla_id"]
            for key in keys_by_talla.get(talla_id, []):
                reservada_map[key] += cls._normalize(row["total"])
        return reservada_map

    @classmethod
    def get_existencia_agregada(cls, almacen, producto, producto_variante):
        fisica = cls._sum_existencia(
            almacen_id=getattr(almacen, "pk", almacen),
            producto_id=getattr(producto, "pk", producto),
            producto_variante_id=getattr(producto_variante, "pk", producto_variante),
        )
        reservada = Decimal("0")
        disponible = fisica - reservada
        if disponible < Decimal("0"):
            disponible = Decimal("0")
        return fisica, reservada, disponible

    @classmethod
    def get_existencia_batch(cls, almacen, tallas):
        """Calcula existencia para un batch de PedidoDetalleTalla.

        Retorna un dict keyed por talla_id con:
        - fisica
        - reservada
        - disponible
        """
        almacen_id = getattr(almacen, "pk", almacen) if almacen else None
        result = {}
        if almacen_id is None or not tallas:
            for talla in tallas:
                talla_id = getattr(talla, "pk", talla)
                result[talla_id] = {
                    "fisica": Decimal("0"),
                    "reservada": Decimal("0"),
                    "disponible": Decimal("0"),
                }
            return result

        existencia_por_key = defaultdict(lambda: Decimal("0"))
        keys_by_talla = defaultdict(list)
        talla_ids = []

        for talla in tallas:
            talla_id = getattr(talla, "pk", talla)
            talla_ids.append(talla_id)
            detalle = getattr(talla, "pedido_detalle", None)
            producto_id = getattr(detalle, "producto_id", None)
            variante_id = getattr(talla, "variante_id", None)
            key = (producto_id, variante_id)
            keys_by_talla[talla_id].append(key)

        # Construimos un queryset único de todas las existencias en el almacén
        # para todos los productos/variantes involucrados y agregamos por key.
        all_keys = {k for sublist in keys_by_talla.values() for k in sublist}
        producto_ids = list({k[0] for k in all_keys})
        variante_ids = list({k[1] for k in all_keys if k[1] is not None})

        existencia_base_qs = Existencia.objects.filter(almacen_id=almacen_id)
        q_cond = models.Q(producto_id__in=producto_ids)
        if variante_ids:
            q_cond = q_cond | models.Q(producto_variante_id__in=variante_ids)

        for row in (
            existencia_base_qs.filter(q_cond)
            .values("producto_id", "producto_variante_id")
            .annotate(total=Sum("cantidad"))
        ):
            key = (row["producto_id"], row["producto_variante_id"])
            existencia_por_key[key] += cls._normalize(row["total"])

        reservada_por_key = cls._sum_reservas_activas(almacen_id, talla_ids, keys_by_talla)

        for talla in tallas:
            talla_id = getattr(talla, "pk", talla)
            keys = keys_by_talla.get(talla_id, [])
            fisica = Decimal("0")
            for key in keys:
                fisica += existencia_por_key[key]
            reservada = Decimal("0")
            for key in keys:
                reservada += reservada_por_key[key]
            disponible = fisica - reservada
            if disponible < Decimal("0"):
                disponible = Decimal("0")
            result[talla_id] = {
                "fisica": fisica,
                "reservada": reservada,
                "disponible": disponible,
            }
        return result
