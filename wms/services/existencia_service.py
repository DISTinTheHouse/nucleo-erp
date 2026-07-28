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
    def _sum_reservas_por_clave(cls, almacen_id, keys):
        """Suma reservas ACTIVAS y APLICADAS en el almacen, agrupadas por
        (producto_id, producto_variante_id).

        - Incluye reservas de CUALQUIER pedido que comparta el stock en el
          mismo almacén (no se limita a las tallas del pedido actual).
        - Incluye tanto reservas ACTIVAS como APLICADAS: ambas consumen stock
          físico disponible para otros pickings nuevos.
        """
        if not keys:
            return defaultdict(lambda: Decimal("0"))
        almacen_id_n = getattr(almacen_id, "pk", almacen_id) if almacen_id else almacen_id
        all_productos = list({k[0] for k in keys if k[0] is not None})
        all_variantes = list({k[1] for k in keys if k[1] is not None})

        base = inventario_reservas.objects.filter(
            almacen_id=almacen_id_n,
            estado__in=[
                inventario_reservas.Estado.ACTIVA,
                inventario_reservas.Estado.APLICADA,
            ],
        )
        q_cond = models.Q(pk=-1)
        if all_productos:
            q_cond = q_cond | models.Q(
                existencia__producto_id__in=all_productos,
                existencia__producto_variante_id__isnull=True,
            ) | models.Q(
                pedido_detalle_talla__variante_id__isnull=True,
                pedido_detalle__producto_id__in=all_productos,
            )
        if all_variantes:
            q_cond = q_cond | models.Q(
                existencia__producto_variante_id__in=all_variantes
            ) | models.Q(
                pedido_detalle_talla__variante_id__in=all_variantes
            )

        por_existencia = defaultdict(lambda: Decimal("0"))
        rows = (
            base.filter(q_cond)
            .values(
                "existencia__producto_id",
                "existencia__producto_variante_id",
            )
            .annotate(total=Sum("cantidad"))
        )
        for row in rows:
            k = (row["existencia__producto_id"], row["existencia__producto_variante_id"])
            por_existencia[k] += cls._normalize(row["total"])

        por_talla = defaultdict(lambda: Decimal("0"))
        rows_talla = (
            base.filter(
                pedido_detalle_talla__isnull=False,
            )
            .values(
                "pedido_detalle_talla__pedido_detalle__producto_id",
                "pedido_detalle_talla__variante_id",
            )
            .annotate(total=Sum("cantidad"))
        )
        for row in rows_talla:
            k = (
                row["pedido_detalle_talla__pedido_detalle__producto_id"],
                row["pedido_detalle_talla__variante_id"],
            )
            por_talla[k] += cls._normalize(row["total"])

        resultado = defaultdict(lambda: Decimal("0"))
        for k in keys:
            resultado[k] = por_existencia.get(k, Decimal("0")) + por_talla.get(k, Decimal("0"))
        return resultado

    @classmethod
    def get_existencia_agregada(cls, almacen, producto, producto_variante):
        almacen_id = getattr(almacen, "pk", almacen)
        producto_id = getattr(producto, "pk", producto)
        producto_variante_id = getattr(producto_variante, "pk", producto_variante)
        fisica = cls._sum_existencia(almacen_id, producto_id, producto_variante_id)
        reservas_map = cls._sum_reservas_por_clave(
            almacen_id, [(producto_id, producto_variante_id)]
        )
        reservada = cls._normalize(reservas_map.get((producto_id, producto_variante_id)))
        disponible = fisica - reservada
        if disponible < Decimal("0"):
            disponible = Decimal("0")
        return fisica, reservada, disponible

    @classmethod
    def get_existencia_batch(cls, almacen, tallas):
        """Calcula existencia para un batch de PedidoDetalleTalla.

        Retorna un dict keyed por talla_id con:
        - fisica
        - reservada (incluye ACTIVAS + APLICADAS de CUALQUIER pedido que
          comparta la misma clave producto/variante en el almacen)
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
        all_keys = set()

        for talla in tallas:
            talla_id = getattr(talla, "pk", talla)
            talla_ids.append(talla_id)
            detalle = getattr(talla, "pedido_detalle", None)
            producto_id = getattr(detalle, "producto_id", None)
            variante_id = getattr(talla, "variante_id", None)
            key = (producto_id, variante_id)
            keys_by_talla[talla_id].append(key)
            all_keys.add(key)

        all_productos = list({k[0] for k in all_keys if k[0] is not None})
        all_variantes = list({k[1] for k in all_keys if k[1] is not None})

        existencia_base_qs = Existencia.objects.filter(almacen_id=almacen_id)
        q_cond = models.Q(producto_id__in=all_productos)
        if all_variantes:
            q_cond = q_cond | models.Q(producto_variante_id__in=all_variantes)

        for row in (
            existencia_base_qs.filter(q_cond)
            .values("producto_id", "producto_variante_id")
            .annotate(total=Sum("cantidad"))
        ):
            key = (row["producto_id"], row["producto_variante_id"])
            existencia_por_key[key] += cls._normalize(row["total"])

        reservada_por_key = cls._sum_reservas_por_clave(almacen_id, list(all_keys))

        for talla in tallas:
            talla_id = getattr(talla, "pk", talla)
            keys = keys_by_talla.get(talla_id, [])
            fisica = Decimal("0")
            reservada = Decimal("0")
            for key in keys:
                fisica += existencia_por_key[key]
                reservada += cls._normalize(reservada_por_key.get(key))
            disponible = fisica - reservada
            if disponible < Decimal("0"):
                disponible = Decimal("0")
            result[talla_id] = {
                "fisica": fisica,
                "reservada": reservada,
                "disponible": disponible,
            }
        return result
