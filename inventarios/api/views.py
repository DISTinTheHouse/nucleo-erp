from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets, permissions
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from inventarios.models import (
    Almacen,
    Ubicacion,
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    AjusteInventario,
)
from catalogo.models import Producto, ProductoVariante
from auditoria.models import AuditoriaEvento
from nucleo.models import Empresa, Sucursal
from .serializers import (
    AlmacenSerializer,
    UbicacionSerializer,
    ExistenciaSerializer,
    MovimientoInventarioSerializer,
    MovimientoInventarioDetalleSerializer,
    AjusteInventarioSerializer,
    AuditoriaMovimientoSerializer,
)

class IsAuthenticatedAndScoped(permissions.BasePermission):
    """
    - Permite lectura a autenticados.
    - Crea/edita solo si es admin de empresa o superuser.
    - Elimina solo si es admin de empresa o superuser.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in permissions.SAFE_METHODS:
            return True
        # Write operations: require admin_empresa or superuser
        return request.user.is_superuser or getattr(request.user, 'is_admin_empresa', False)

    def has_object_permission(self, request, view, obj):
        # Lectura ya pasó por has_permission
        if request.method in permissions.SAFE_METHODS:
            return True
        # Para write/delete, mismo criterio: admin_empresa o superuser
        return request.user.is_superuser or getattr(request.user, 'is_admin_empresa', False)

class AlmacenViewSet(viewsets.ModelViewSet):
    queryset = Almacen.objects.all().select_related('empresa', 'sucursal')
    serializer_class = AlmacenSerializer
    permission_classes = [IsAuthenticatedAndScoped]
    lookup_field = 'id_almacen'

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        # Filtrar por empresas y sucursales permitidas
        empresa_ids = []
        if user.empresa_id:
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list('pk', flat=True))
        sucursal_ids = list(user.sucursales.values_list('pk', flat=True))
        return qs.filter(
            models.Q(empresa_id__in=empresa_ids) &
            models.Q(sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        instance = serializer.save()
        # Verificación final de alcance: empresa/sucursal dentro del scope del usuario
        if not user.is_superuser:
            if instance.sucursal_id and not user.sucursales.filter(pk=instance.sucursal_id).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            if instance.empresa_id:
                if user.empresa_id and instance.empresa_id != user.empresa_id and not user.empresas.filter(pk=instance.empresa_id).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")

class UbicacionViewSet(viewsets.ModelViewSet):
    queryset = Ubicacion.objects.all().select_related('almacen__empresa', 'almacen__sucursal')
    serializer_class = UbicacionSerializer
    permission_classes = [IsAuthenticatedAndScoped]
    lookup_field = 'id_ubicacion'

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        empresa_ids = []
        if user.empresa_id:
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list('pk', flat=True))
        sucursal_ids = list(user.sucursales.values_list('pk', flat=True))
        return qs.filter(
            models.Q(almacen__empresa_id__in=empresa_ids) &
            models.Q(almacen__sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        # Obtener el almacén del validated_data
        almacen = serializer.validated_data.get('almacen')
        
        if not user.is_superuser and almacen:
            sucursal = almacen.sucursal
            empresa = almacen.empresa
            
            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if user.empresa_id and empresa.pk != user.empresa_id and not user.empresas.filter(pk=empresa.pk).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()

class ReporteExistenciasPeriodoPagination(PageNumberPagination):
    # Instantiated explicitly inside the action, not set as pagination_class
    # on the ViewSet, so list()/other actions are unaffected.
    page_size = 200
    page_size_query_param = "page_size"
    max_page_size = 2000


class ExistenciaViewSet(viewsets.ModelViewSet):
    queryset = Existencia.objects.all().select_related(
        "producto",
        "producto__tipo",
        "producto_variante__producto",
        "producto_variante__producto__tipo",
        "producto_variante__color",
        "producto_variante__talla",
        "almacen",
        "ubicacion",
        "ubicacion__almacen",
    )
    serializer_class = ExistenciaSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def get_queryset(self):
        def to_int(v):
            if v in (None, ""): return None
            try:
                return int(v)
            except Exception:
                return None
            
        qs = self.queryset
        qp = self.request.query_params

        empresa_id = to_int(qp.get("empresa_id") or qp.get("empresa"))
        sucursal_id = to_int(qp.get("sucursal_id") or qp.get("sucursal"))
        almacen_id = to_int(qp.get("almacen_id") or qp.get("almacen"))
        producto_variante_id = to_int(qp.get("producto_variante_id") or qp.get("producto_variante"))
        producto_id = to_int(qp.get("producto_id") or qp.get("producto"))
        color_id = to_int(qp.get("color_id") or qp.get("color"))
        talla_id = to_int(qp.get("talla_id") or qp.get("talla"))
        sku_q = (qp.get("sku") or qp.get("q") or "").strip()
        almacen_id = to_int(qp.get("almacen_id") or qp.get("almacen"))

        if empresa_id: qs = qs.filter(almacen__empresa_id=empresa_id)
        if sucursal_id: qs = qs.filter(almacen__sucursal_id=sucursal_id)
        if almacen_id: qs = qs.filter(almacen_id=almacen_id)
        if producto_variante_id: qs = qs.filter(producto_variante_id=producto_variante_id)
        if almacen_id: qs = qs.filter(almacen_id=almacen_id)
        if producto_id:
            qs = qs.filter(
                models.Q(producto_id=producto_id) |
                models.Q(producto_variante__producto_id=producto_id)
            )
        if color_id: qs = qs.filter(producto_variante__color_id=color_id)
        if talla_id: qs = qs.filter(producto_variante__talla_id=talla_id)
        if sku_q: qs = qs.filter(producto_variante__sku__icontains=sku_q)

        limit_raw = (qp.get("limit") or "").strip()
        limit = None
        if limit_raw.lower() in {"all", "0", "-1"}:
            limit = None
        else:
            try:
                limit = int(limit_raw) if limit_raw else 200
            except Exception:
                limit = 200

        qs = qs.order_by("-id")
        if limit is None:
            return qs
        limit = max(1, min(limit, 2000))
        return qs[:limit]

    def perform_create(self, serializer):
        user = self.request.user
        instance = serializer.save()
        if not user.is_superuser:
            # Validar acceso al almacén asociado
            almacen = instance.almacen
            if almacen:
                if almacen.sucursal_id and not user.sucursales.filter(pk=almacen.sucursal_id).exists():
                    raise PermissionDenied("No tiene acceso a la sucursal de este almacén")
                if almacen.empresa_id:
                    if user.empresa_id and almacen.empresa_id != user.empresa_id and not user.empresas.filter(pk=almacen.empresa_id).exists():
                        raise PermissionDenied("No tiene acceso a la empresa de este almacén")

    def _report_to_int(self, value):
        if value in (None, ""):
            return None
        try:
            return int(value)
        except Exception:
            return None

    def _report_to_decimal(self, value):
        try:
            return Decimal(str(value or 0))
        except Exception:
            return Decimal("0")

    def _quantize_qty(self, value):
        return self._report_to_decimal(value).quantize(Decimal("0.0001"))

    def _quantize_money(self, value):
        return self._report_to_decimal(value).quantize(Decimal("0.01"))

    def _build_report_almacenes_queryset(self):
        user = self.request.user
        qp = self.request.query_params
        qs = Almacen.objects.select_related("empresa", "sucursal").all()

        if not user.is_superuser:
            empresa_ids = []
            if getattr(user, "empresa_id", None):
                empresa_ids.append(user.empresa_id)
            empresa_ids += list(user.empresas.values_list("pk", flat=True))
            sucursal_ids = list(user.sucursales.values_list("pk", flat=True))
            qs = qs.filter(
                models.Q(empresa_id__in=empresa_ids)
                & models.Q(sucursal_id__in=sucursal_ids)
            )

        empresa_id = self._report_to_int(qp.get("empresa") or qp.get("empresa_id"))
        sucursal_id = self._report_to_int(qp.get("sucursal") or qp.get("sucursal_id"))
        almacen_id = self._report_to_int(qp.get("almacen") or qp.get("almacen_id"))

        if empresa_id:
            qs = qs.filter(empresa_id=empresa_id)
        if sucursal_id:
            qs = qs.filter(sucursal_id=sucursal_id)
        if almacen_id:
            qs = qs.filter(pk=almacen_id)
        return qs

    def _parse_report_dates(self):
        fecha_inicio_raw = (self.request.query_params.get("fecha_inicio") or "").strip()
        fecha_final_raw = (self.request.query_params.get("fecha_final") or "").strip()
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

    def _iter_auditoria_items(self, eventos, allowed_almacen_ids, producto_id=None, producto_variante_id=None):
        for evento in eventos.iterator():
            payload = evento.despues_json or evento.antes_json or {}
            for item in payload.get("items", []) or []:
                almacen_id = self._report_to_int(item.get("almacen_id"))
                if not almacen_id or almacen_id not in allowed_almacen_ids:
                    continue

                item_producto_id = self._report_to_int(item.get("producto_id"))
                item_variante_id = self._report_to_int(item.get("producto_variante_id"))

                if producto_variante_id and item_variante_id != producto_variante_id:
                    continue
                if producto_id and item_producto_id != producto_id:
                    continue

                yield {
                    "almacen_id": almacen_id,
                    "producto_id": item_producto_id,
                    "producto_variante_id": item_variante_id,
                    "delta": self._report_to_decimal(item.get("delta")),
                }

    def _movement_almacen_ids(self, detalle):
        movimiento = detalle.movimiento_inventario
        origen_id = (
            getattr(detalle.ubicacion_origen, "almacen_id", None)
            or getattr(getattr(movimiento, "transferencia", None), "almacen_origen_id", None)
            or getattr(getattr(movimiento, "ajuste_inventario", None), "almacen_id", None)
        )
        destino_id = (
            getattr(detalle.ubicacion_destino, "almacen_id", None)
            or getattr(getattr(movimiento, "recepcion", None), "almacen_id", None)
            or getattr(getattr(movimiento, "transferencia", None), "almacen_destino_id", None)
            or getattr(getattr(movimiento, "ajuste_inventario", None), "almacen_id", None)
        )
        return origen_id, destino_id

    @action(detail=False, methods=["get"], url_path="reporte-existencias-periodo")
    def reporte_existencias_periodo(self, request):
        fecha_inicio, fecha_final, inicio_dt, final_dt = self._parse_report_dates()
        qp = request.query_params
        almacen_id = self._report_to_int(qp.get("almacen") or qp.get("almacen_id"))
        producto_id = self._report_to_int(qp.get("producto") or qp.get("producto_id"))
        producto_variante_id = self._report_to_int(
            qp.get("producto_variante") or qp.get("producto_variante_id")
        )

        almacenes_qs = self._build_report_almacenes_queryset()
        almacenes = list(
            almacenes_qs.values(
                "id_almacen",
                "codigo",
                "nombre",
                "empresa_id",
                "sucursal_id",
            )
        )
        allowed_almacen_ids = {row["id_almacen"] for row in almacenes}

        if not allowed_almacen_ids:
            return Response(
                {
                    "fecha_inicio": str(fecha_inicio),
                    "fecha_final": str(fecha_final),
                    "filtros": {
                        "producto_id": producto_id,
                        "producto_variante_id": producto_variante_id,
                    },
                    "resumen": {
                        "existencia_inicial": "0.0000",
                        "entradas": "0.0000",
                        "salidas": "0.0000",
                        "existencia_final": "0.0000",
                        "costo_total_existencia_final": "0.00",
                    },
                    "resumen_por_almacen": [],
                    "count": 0,
                    "next": None,
                    "previous": None,
                    "results": [],
                },
                status=status.HTTP_200_OK,
            )

        almacen_map = {row["id_almacen"]: row for row in almacenes}
        empresa_ids = {row["empresa_id"] for row in almacenes if row["empresa_id"]}

        current_map = defaultdict(lambda: Decimal("0"))
        keys = set()

        current_qs = Existencia.objects.select_related(
            "producto",
            "producto_variante",
            "producto_variante__producto",
            "producto_variante__color",
            "producto_variante__talla",
            "almacen",
        ).filter(almacen_id__in=allowed_almacen_ids)

        if producto_variante_id:
            current_qs = current_qs.filter(producto_variante_id=producto_variante_id)
        if producto_id:
            current_qs = current_qs.filter(
                models.Q(producto_id=producto_id)
                | models.Q(producto_variante__producto_id=producto_id)
            )

        for ex in current_qs:
            key = (
                ex.almacen_id,
                getattr(ex, "producto_id", None)
                or getattr(getattr(ex, "producto_variante", None), "producto_id", None),
                ex.producto_variante_id,
            )
            current_map[key] += self._report_to_decimal(ex.cantidad)
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
        for item in self._iter_auditoria_items(
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
        for item in self._iter_auditoria_items(
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
                MovimientoInventarioDetalle.objects.select_related(
                    "movimiento_inventario",
                    "movimiento_inventario__recepcion",
                    "movimiento_inventario__ajuste_inventario",
                    "movimiento_inventario__transferencia",
                    "ubicacion_origen",
                    "ubicacion_destino",
                )
                .filter(
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

            for detalle in movement_cost_qs:
                producto_key_id = detalle.producto_id
                variante_key_id = detalle.producto_variante_id
                origen_id, destino_id = self._movement_almacen_ids(detalle)
                for almacen_id in {origen_id, destino_id}:
                    key = (almacen_id, producto_key_id, variante_key_id)
                    if almacen_id and key in keys and key not in cost_map:
                        cost_map[key] = self._report_to_decimal(detalle.costo_unitario)
                if len(cost_map) >= len(keys):
                    break

        detalle = []
        resumen_por_almacen = defaultdict(
            lambda: {
                "existencia_inicial": Decimal("0"),
                "entradas": Decimal("0"),
                "salidas": Decimal("0"),
                "existencia_final": Decimal("0"),
                "costo_total_existencia_final": Decimal("0"),
            }
        )
        resumen_total = {
            "existencia_inicial": Decimal("0"),
            "entradas": Decimal("0"),
            "salidas": Decimal("0"),
            "existencia_final": Decimal("0"),
            "costo_total_existencia_final": Decimal("0"),
        }

        for key in sorted(keys, key=lambda row: (row[0], row[1] or 0, row[2] or 0)):
            almacen_id, producto_key_id, variante_key_id = key
            current_qty = current_map[key]
            period_delta = period_delta_map[key]
            entradas = period_in_map[key]
            salidas = period_out_map[key]
            existencia_final = current_qty - post_end_delta_map[key]
            existencia_inicial = existencia_final - period_delta
            costo_unitario = cost_map.get(key, Decimal("0"))
            costo_existencia_final = self._quantize_money(existencia_final * costo_unitario)

            variante = variantes_map.get(variante_key_id)
            producto = variantes_map[variante_key_id].producto if variante else productos_map.get(producto_key_id)
            almacen = almacen_map.get(almacen_id, {})

            row = {
                "almacen_id": almacen_id,
                "almacen_codigo": almacen.get("codigo"),
                "almacen_nombre": almacen.get("nombre"),
                "producto_id": producto_key_id,
                "producto_variante_id": variante_key_id,
                "producto_nombre": getattr(producto, "nombre", None),
                "sku": getattr(variante, "sku", None) if variante else None,
                "color": getattr(getattr(variante, "color", None), "nombre", None) if variante else None,
                "talla": getattr(getattr(variante, "talla", None), "nombre", None) if variante else None,
                "existencia_inicial": str(self._quantize_qty(existencia_inicial)),
                "entradas": str(self._quantize_qty(entradas)),
                "salidas": str(self._quantize_qty(salidas)),
                "existencia_final": str(self._quantize_qty(existencia_final)),
                "costo_unitario_final": str(self._quantize_money(costo_unitario)),
                "costo_existencia_final": str(costo_existencia_final),
            }
            detalle.append(row)

            resumen_por_almacen[almacen_id]["existencia_inicial"] += existencia_inicial
            resumen_por_almacen[almacen_id]["entradas"] += entradas
            resumen_por_almacen[almacen_id]["salidas"] += salidas
            resumen_por_almacen[almacen_id]["existencia_final"] += existencia_final
            resumen_por_almacen[almacen_id]["costo_total_existencia_final"] += self._report_to_decimal(
                costo_existencia_final
            )

            resumen_total["existencia_inicial"] += existencia_inicial
            resumen_total["entradas"] += entradas
            resumen_total["salidas"] += salidas
            resumen_total["existencia_final"] += existencia_final
            resumen_total["costo_total_existencia_final"] += self._report_to_decimal(
                costo_existencia_final
            )

        resumen_almacenes_payload = []
        for almacen_id, totals in sorted(resumen_por_almacen.items(), key=lambda item: item[0]):
            almacen = almacen_map.get(almacen_id, {})
            resumen_almacenes_payload.append(
                {
                    "almacen_id": almacen_id,
                    "almacen_codigo": almacen.get("codigo"),
                    "almacen_nombre": almacen.get("nombre"),
                    "existencia_inicial": str(self._quantize_qty(totals["existencia_inicial"])),
                    "entradas": str(self._quantize_qty(totals["entradas"])),
                    "salidas": str(self._quantize_qty(totals["salidas"])),
                    "existencia_final": str(self._quantize_qty(totals["existencia_final"])),
                    "costo_total_existencia_final": str(
                        self._quantize_money(totals["costo_total_existencia_final"])
                    ),
                }
            )

        paginator = ReporteExistenciasPeriodoPagination()
        page = paginator.paginate_queryset(detalle, request, view=self)

        response = paginator.get_paginated_response(page)
        response.data["fecha_inicio"] = str(fecha_inicio)
        response.data["fecha_final"] = str(fecha_final)
        response.data["filtros"] = {
            "almacen_id": almacen_id,
        }
        response.data["resumen"] = {
            "existencia_inicial": str(self._quantize_qty(resumen_total["existencia_inicial"])),
            "entradas": str(self._quantize_qty(resumen_total["entradas"])),
            "salidas": str(self._quantize_qty(resumen_total["salidas"])),
            "existencia_final": str(self._quantize_qty(resumen_total["existencia_final"])),
            "costo_total_existencia_final": str(
                self._quantize_money(resumen_total["costo_total_existencia_final"])
            ),
        }
        response.data["resumen_por_almacen"] = resumen_almacenes_payload
        return response


class OperacionInventarioViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticatedAndScoped]

    def _to_int(self, v):
        if v in (None, ""):
            return None
        try:
            return int(v)
        except Exception:
            return None

    def _to_decimal(self, v):
        if v in (None, ""):
            return None
        try:
            return Decimal(str(v))
        except Exception:
            return None

    def _get_almacen(self, request):
        almacen_id = self._to_int(request.data.get("almacen") or request.data.get("almacen_id"))
        if not almacen_id:
            raise ValidationError({"almacen": "Almacén es requerido."})
        almacen = (
            Almacen.objects.select_related("empresa", "sucursal")
            .filter(pk=almacen_id)
            .first()
        )
        if not almacen:
            raise ValidationError({"almacen": "Almacén no encontrado."})
        return almacen

    def _get_items(self, request):
        items = request.data.get("items") or request.data.get("detalle") or []
        if not isinstance(items, list) or not items:
            raise ValidationError({"items": "items debe ser una lista no vacía."})
        normalized = []
        producto_ids = set()
        producto_variante_ids = set()
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                raise ValidationError({"items": f"Item #{idx+1} inválido."})
            producto_id = self._to_int(it.get("producto") or it.get("producto_id"))
            pv_id = self._to_int(it.get("producto_variante") or it.get("producto_variante_id"))
            if not producto_id and not pv_id:
                raise ValidationError(
                    {"items": f"Item #{idx+1}: producto o producto_variante es requerido."}
                )
            qty = self._to_decimal(it.get("cantidad"))
            if qty is None:
                raise ValidationError({"items": f"Item #{idx+1}: cantidad inválida."})
            ubicacion_id = self._to_int(it.get("ubicacion") or it.get("ubicacion_id"))
            lote_id = self._to_int(it.get("lote") or it.get("lote_id"))
            serie_id = self._to_int(it.get("serie") or it.get("serie_id"))
            if producto_id:
                producto_ids.add(producto_id)
            if pv_id:
                producto_variante_ids.add(pv_id)
            normalized.append(
                {
                    "producto_id": producto_id,
                    "producto_variante_id": pv_id,
                    "cantidad": qty,
                    "ubicacion_id": ubicacion_id,
                    "lote_id": lote_id,
                    "serie_id": serie_id,
                }
            )

        variantes = {
            variante.pk: variante
            for variante in ProductoVariante.objects.filter(pk__in=producto_variante_ids).only("id", "producto_id")
        }
        producto_ids.update(
            variante.producto_id for variante in variantes.values() if getattr(variante, "producto_id", None)
        )
        productos = {
            producto.pk: producto
            for producto in Producto.objects.filter(pk__in=producto_ids).only("id")
        }

        for idx, item in enumerate(normalized):
            variante_id = item["producto_variante_id"]
            producto_id = item["producto_id"]

            if variante_id:
                variante = variantes.get(variante_id)
                if not variante:
                    raise ValidationError(
                        {"items": f"Item #{idx+1}: producto_variante no encontrado."}
                    )
                if producto_id and variante.producto_id != producto_id:
                    raise ValidationError(
                        {
                            "items": (
                                f"Item #{idx+1}: producto y producto_variante no coinciden."
                            )
                        }
                    )
                item["producto_id"] = variante.producto_id

            if not item["producto_id"] or item["producto_id"] not in productos:
                raise ValidationError({"items": f"Item #{idx+1}: producto no encontrado."})
        return normalized

    def _resolve_empresa_sucursal(self, request, almacen):
        user = request.user
        empresa = (
            getattr(almacen, "empresa", None)
            or getattr(user, "empresa", None)
            or Empresa.objects.filter(
                pk=self._to_int(request.data.get("empresa") or request.data.get("empresa_id"))
            ).first()
        )
        sucursal = (
            getattr(almacen, "sucursal", None)
            or getattr(user, "sucursal_default", None)
            or Sucursal.objects.filter(
                pk=self._to_int(request.data.get("sucursal") or request.data.get("sucursal_id"))
            ).first()
        )
        return empresa, sucursal

    def _crear_movimiento_formal(self, request, tipo, almacen, ajuste_id, detalle_movimientos):
        empresa, sucursal = self._resolve_empresa_sucursal(request, almacen)
        if not empresa or not sucursal:
            return None

        movimiento = MovimientoInventario.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            pedido_id=None,
            entrega_id=None,
            devolucion_id=None,
            ajuste_inventario_id=ajuste_id,
            tipo_movimiento=tipo,
            usuario=request.user,
            observaciones=(request.data.get("observaciones") or request.data.get("motivo") or None),
            recepcion_id=None,
            transferencia_id=None,
            op_id=None,
        )

        for item in detalle_movimientos:
            MovimientoInventarioDetalle.objects.create(
                movimiento_inventario=movimiento,
                producto_id=item["producto_id"],
                ubicacion_origen_id=item["ubicacion_origen_id"],
                ubicacion_destino_id=item["ubicacion_destino_id"],
                lote_id=item["lote_id"],
                serie_id=item["serie_id"],
                cantidad=item["cantidad"],
                costo_unitario=Decimal("0"),
            )
        return movimiento

    def _apply(self, request, tipo):
        tipo = (tipo or "").strip().upper()
        if tipo not in {"ENTRADA", "SALIDA", "AJUSTE"}:
            raise ValidationError({"tipo": "Tipo inválido."})

        almacen = self._get_almacen(request)
        items = self._get_items(request)

        if tipo in {"ENTRADA", "SALIDA"}:
            for it in items:
                if it["cantidad"] <= 0:
                    raise ValidationError({"items": "cantidad debe ser > 0 para entradas/salidas."})
        elif tipo == "AJUSTE":
            for it in items:
                if it["cantidad"] < 0:
                    raise ValidationError({"items": "En ajuste, cantidad debe ser >= 0 (cantidad final)."})

        ajuste_id = None
        if tipo == "AJUSTE":
            empresa_obj = almacen.empresa
            sucursal_obj = almacen.sucursal

            if not empresa_obj:
                empresa_id = self._to_int(request.data.get("empresa") or request.data.get("empresa_id"))
                if empresa_id:
                    empresa_obj = Empresa.objects.filter(pk=empresa_id).first()
            if not sucursal_obj:
                sucursal_id = self._to_int(request.data.get("sucursal") or request.data.get("sucursal_id"))
                if sucursal_id:
                    sucursal_obj = Sucursal.objects.filter(pk=sucursal_id).first()

            if empresa_obj and sucursal_obj:
                motivo = (request.data.get("motivo") or "Ajuste").strip()[:100]
                observaciones = (request.data.get("observaciones") or "").strip()[:150] or None
                ajuste = AjusteInventario.objects.create(
                    empresa=empresa_obj,
                    sucursal=sucursal_obj,
                    almacen=almacen,
                    usuario=request.user,
                    motivo=motivo,
                    observaciones=observaciones,
                )
                ajuste_id = ajuste.pk

        user = request.user
        results = []
        before_after = []
        detalle_movimientos = []
        with transaction.atomic():
            for it in items:
                ubicacion = None
                if it["ubicacion_id"]:
                    ubicacion = Ubicacion.objects.filter(
                        pk=it["ubicacion_id"], almacen_id=almacen.pk
                    ).first()
                    if not ubicacion:
                        raise ValidationError({"ubicacion": "Ubicación inválida para el almacén."})

                ex = (
                    Existencia.objects.select_for_update()
                    .filter(almacen_id=almacen.pk, ubicacion_id=(ubicacion.pk if ubicacion else None))
                    .order_by("id")
                )
                if it["producto_variante_id"]:
                    ex = ex.filter(producto_variante_id=it["producto_variante_id"])
                else:
                    ex = ex.filter(
                        producto_id=it["producto_id"],
                        producto_variante__isnull=True,
                    )
                ex = ex.first()
                if not ex and tipo == "SALIDA":
                    fallback = (
                        Existencia.objects.select_for_update()
                        .filter(almacen_id=almacen.pk)
                        .order_by("-cantidad", "id")
                    )
                    if it["producto_variante_id"]:
                        fallback = fallback.filter(producto_variante_id=it["producto_variante_id"])
                    else:
                        fallback = fallback.filter(
                            producto_id=it["producto_id"],
                            producto_variante__isnull=True,
                        )
                    fallback = fallback.first()
                    ex = fallback

                if not ex:
                    if tipo == "SALIDA":
                        raise ValidationError(
                            {"cantidad": "No hay existencia para realizar la salida."}
                        )
                    ex = Existencia.objects.create(
                        producto_id=it["producto_id"],
                        producto_variante_id=it["producto_variante_id"],
                        almacen=almacen,
                        ubicacion=ubicacion,
                        stock=0,
                        cantidad=Decimal("0"),
                    )

                current = ex.cantidad or Decimal("0")
                qty = it["cantidad"]
                ubicacion_origen_id = ex.ubicacion_id if tipo in {"SALIDA", "AJUSTE"} else None
                ubicacion_destino_id = ex.ubicacion_id if tipo == "ENTRADA" else None

                if tipo == "ENTRADA":
                    new_qty = current + qty
                elif tipo == "SALIDA":
                    new_qty = current - qty
                else:
                    new_qty = qty
                    ubicacion_destino_id = ex.ubicacion_id

                if new_qty < 0:
                    raise ValidationError({"cantidad": "La operación deja stock negativo."})

                ex.cantidad = new_qty
                try:
                    ex.stock = int(new_qty)
                except Exception:
                    ex.stock = ex.stock or 0
                ex.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])

                before_after.append(
                    {
                        "producto_id": ex.producto_id,
                        "producto_variante_id": ex.producto_variante_id,
                        "ubicacion_id": ex.ubicacion_id,
                        "cantidad_before": str(current),
                        "cantidad_after": str(new_qty),
                        "delta": str(new_qty - current),
                    }
                )
                detalle_movimientos.append(
                    {
                        "producto_id": ex.producto_id,
                        "ubicacion_origen_id": ubicacion_origen_id,
                        "ubicacion_destino_id": ubicacion_destino_id,
                        "lote_id": it["lote_id"],
                        "serie_id": it["serie_id"],
                        "cantidad": qty if tipo != "AJUSTE" else new_qty,
                    }
                )
                results.append(
                    {
                        "id": ex.pk,
                        "producto_id": ex.producto_id,
                        "producto_variante_id": ex.producto_variante_id,
                        "almacen_id": ex.almacen_id,
                        "ubicacion_id": ex.ubicacion_id,
                        "cantidad": str(ex.cantidad),
                        "stock": ex.stock,
                    }
                )

            empresa_evt = (
                getattr(almacen, "empresa", None)
                or getattr(user, "empresa", None)
                or Empresa.objects.filter(
                    pk=self._to_int(request.data.get("empresa") or request.data.get("empresa_id"))
                ).first()
            )
            if empresa_evt:
                ip = request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR")
                ua = request.META.get("HTTP_USER_AGENT")
                ev = AuditoriaEvento.objects.create(
                    empresa=empresa_evt,
                    usuario=user if getattr(user, "pk", None) else None,
                    modulo="inventarios",
                    accion=tipo,
                    tabla="existencias",
                    id_registro=str(almacen.pk),
                    antes_json={"items": before_after, "ajuste_id": ajuste_id},
                    despues_json={
                        "almacen_id": almacen.pk,
                        "sucursal_id": almacen.sucursal_id,
                        "empresa_id": almacen.empresa_id,
                        "items": before_after,
                        "ajuste_id": ajuste_id,
                    },
                    ip=ip,
                    user_agent=ua,
                )
            else:
                ev = None

            movimiento_formal = self._crear_movimiento_formal(
                request=request,
                tipo=tipo,
                almacen=almacen,
                ajuste_id=ajuste_id,
                detalle_movimientos=detalle_movimientos,
            )

        return Response(
            {
                "tipo": tipo,
                "almacen_id": almacen.pk,
                "ajuste_id": ajuste_id,
                "movimiento_id": (getattr(ev, "id_evento", None) if ev else None),
                "movimiento_inventario_id": (getattr(movimiento_formal, "pk", None) if movimiento_formal else None),
                "result": results,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="entrada")
    def entrada(self, request):
        return self._apply(request, "ENTRADA")

    @action(detail=False, methods=["post"], url_path="salida")
    def salida(self, request):
        return self._apply(request, "SALIDA")

    @action(detail=False, methods=["post"], url_path="ajuste")
    def ajuste(self, request):
        return self._apply(request, "AJUSTE")


class MovimientoOperacionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditoriaMovimientoSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def _base_queryset(self):
        return (
            AuditoriaEvento.objects.filter(modulo="inventarios", tabla="existencias")
            .select_related("empresa", "usuario")
            .order_by("-created_at", "-id_evento")
        )

    def get_queryset(self):
        qs = self._base_queryset()
        qp = self.request.query_params
        try:
            empresa_id = int(qp.get("empresa_id") or qp.get("empresa") or 0)
        except Exception:
            empresa_id = 0
        if empresa_id:
            qs = qs.filter(empresa_id=empresa_id)
        accion = (qp.get("accion") or qp.get("tipo") or "").strip().upper()
        if accion in {"ENTRADA", "SALIDA", "AJUSTE"}:
            qs = qs.filter(accion=accion)
        return qs

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        limit_raw = (request.query_params.get("limit") or "").strip()
        try:
            limit = int(limit_raw) if limit_raw else 200
        except Exception:
            limit = 200
        limit = max(1, min(limit, 2000))

        serializer = self.get_serializer(queryset[:limit], many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["get"], url_path="detalles")
    def detalles(self, request, pk=None):
        movimiento = self.get_queryset().filter(id_evento=pk).first()
        if not movimiento:
            raise ValidationError({"movimiento": "Movimiento no encontrado."})

        before_items = (movimiento.antes_json or {}).get("items") or []
        after_items = (movimiento.despues_json or {}).get("items") or []
        items = after_items or before_items

        despues = movimiento.despues_json or {}
        almacen_id = despues.get("almacen_id")
        sucursal_id = despues.get("sucursal_id")

        def _safe_int(v):
            try:
                return int(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        # Single lookups for top-level names (one record — no N+1 risk)
        almacen_obj = (
            Almacen.objects.filter(pk=almacen_id).only("nombre").first()
            if almacen_id else None
        )
        sucursal_obj = (
            Sucursal.objects.filter(pk=sucursal_id).only("nombre").first()
            if sucursal_id else None
        )
        empresa_nombre = getattr(movimiento.empresa, "razon_social", None)

        # Collect IDs from all items for a single bulk fetch each (avoids N+1)
        variante_ids, producto_ids, ubicacion_ids = set(), set(), set()
        for it in items:
            if not isinstance(it, dict):
                continue
            v = _safe_int(it.get("producto_variante_id") or it.get("producto_variante"))
            p = _safe_int(it.get("producto_id") or it.get("producto"))
            u = _safe_int(it.get("ubicacion_id") or it.get("ubicacion"))
            if v:
                variante_ids.add(v)
            if p:
                producto_ids.add(p)
            if u:
                ubicacion_ids.add(u)

        variantes = {
            obj.pk: obj
            for obj in ProductoVariante.objects.filter(pk__in=variante_ids).only("id", "nombre")
        } if variante_ids else {}
        productos = {
            obj.pk: obj
            for obj in Producto.objects.filter(pk__in=producto_ids).only("id", "nombre")
        } if producto_ids else {}
        ubicaciones = {
            obj.pk: obj
            for obj in Ubicacion.objects.select_related("almacen").filter(pk__in=ubicacion_ids)
        } if ubicacion_ids else {}

        enriched_items = []
        for it in items:
            if not isinstance(it, dict):
                enriched_items.append(it)
                continue
            item = dict(it)
            v_id = _safe_int(it.get("producto_variante_id") or it.get("producto_variante"))
            p_id = _safe_int(it.get("producto_id") or it.get("producto"))
            u_id = _safe_int(it.get("ubicacion_id") or it.get("ubicacion"))

            if v_id and v_id in variantes:
                item["producto_nombre"] = variantes[v_id].nombre
            elif p_id and p_id in productos:
                item["producto_nombre"] = productos[p_id].nombre
            else:
                item["producto_nombre"] = None

            item["ubicacion_nombre"] = (
                str(ubicaciones[u_id]) if u_id and u_id in ubicaciones else None
            )
            enriched_items.append(item)

        return Response(
            {
                "id": movimiento.id_evento,
                "tipo_movimiento": movimiento.accion,
                "fecha": movimiento.created_at,
                "usuario": getattr(movimiento.usuario, "pk", None),
                "usuario_nombre": (
                    movimiento.usuario.get_full_name().strip() or movimiento.usuario.email
                    if getattr(movimiento, "usuario", None)
                    else None
                ),
                "almacen_id": almacen_id,
                "almacen_nombre": getattr(almacen_obj, "nombre", None),
                "sucursal_id": sucursal_id,
                "sucursal_nombre": getattr(sucursal_obj, "nombre", None),
                "empresa_id": movimiento.empresa_id,
                "empresa_nombre": empresa_nombre,
                "detalle_count": len(enriched_items),
                "detalle": enriched_items,
                "antes_json": movimiento.antes_json,
                "despues_json": movimiento.despues_json,
            },
            status=status.HTTP_200_OK,
        )

class MovimientoInventarioViewSet(viewsets.ModelViewSet):
    queryset = MovimientoInventario.objects.filter(activo=True).select_related('empresa', 'sucursal', 'pedido', 'entrega', 'devolucion', 'ajuste_inventario')
    serializer_class = MovimientoInventarioSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        
        empresa_ids = []
        if user.empresa_id:
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list('pk', flat=True))
        sucursal_ids = list(user.sucursales.values_list('pk', flat=True))
        
        return qs.filter(
            models.Q(empresa_id__in=empresa_ids) &
            models.Q(sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_superuser:
            sucursal = serializer.validated_data.get('sucursal')
            empresa = serializer.validated_data.get('empresa')

            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if user.empresa_id and empresa.pk != user.empresa_id and not user.empresas.filter(pk=empresa.pk).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()

    def perform_destroy(self, instance):
        instance.soft_delete()


class MovimientoInventarioDetalleViewSet(viewsets.ModelViewSet):
    queryset = MovimientoInventarioDetalle.objects.all().select_related('movimiento_inventario', 'producto', 'ubicacion_origen', 'ubicacion_destino', 'lote', 'serie')
    serializer_class = MovimientoInventarioDetalleSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        
        empresa_ids = []
        if user.empresa_id:
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list('pk', flat=True))
        sucursal_ids = list(user.sucursales.values_list('pk', flat=True))
        
        return qs.filter(
            models.Q(movimiento_inventario__empresa_id__in=empresa_ids) &
            models.Q(movimiento_inventario__sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_superuser:
            movimiento = serializer.validated_data.get('movimiento_inventario')
            if movimiento:
                if movimiento.sucursal_id and not user.sucursales.filter(pk=movimiento.sucursal_id).exists():
                    raise PermissionDenied("No tiene acceso a la sucursal del movimiento de inventario")
                if movimiento.empresa_id:
                    if user.empresa_id and movimiento.empresa_id != user.empresa_id and not user.empresas.filter(pk=movimiento.empresa_id).exists():
                        raise PermissionDenied("No tiene acceso a la empresa del movimiento de inventario")
        serializer.save()

class AjusteInventarioViewSet(viewsets.ModelViewSet):
    queryset = AjusteInventario.objects.filter(activo=True).select_related('empresa', 'sucursal', 'almacen')
    serializer_class = AjusteInventarioSerializer
    permission_classes = [IsAuthenticatedAndScoped]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset
        if user.is_superuser:
            return qs
        
        empresa_ids = []
        if user.empresa_id:
            empresa_ids.append(user.empresa_id)
        empresa_ids += list(user.empresas.values_list('pk', flat=True))
        sucursal_ids = list(user.sucursales.values_list('pk', flat=True))
        
        return qs.filter(
            models.Q(empresa_id__in=empresa_ids) &
            models.Q(sucursal_id__in=sucursal_ids)
        ).distinct()

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_superuser:
            sucursal = serializer.validated_data.get('sucursal')
            empresa = serializer.validated_data.get('empresa')
            
            if sucursal and not user.sucursales.filter(pk=sucursal.pk).exists():
                raise PermissionDenied("No tiene acceso a esta sucursal")
            
            if empresa:
                if user.empresa_id and empresa.pk != user.empresa_id and not user.empresas.filter(pk=empresa.pk).exists():
                    raise PermissionDenied("No tiene acceso a esta empresa")
        serializer.save()
    
    def perform_destroy(self, instance):
        instance.soft_delete()
