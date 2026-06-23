from decimal import Decimal

from django.db import models, transaction
from rest_framework import status, viewsets, permissions
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
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
        qs = self.queryset

        qp = self.request.query_params

        def to_int(v):
            if v in (None, ""):
                return None
            try:
                return int(v)
            except Exception:
                return None

        empresa_id = to_int(qp.get("empresa_id") or qp.get("empresa"))
        sucursal_id = to_int(qp.get("sucursal_id") or qp.get("sucursal"))
        almacen_id = to_int(qp.get("almacen_id") or qp.get("almacen"))
        producto_variante_id = to_int(
            qp.get("producto_variante_id") or qp.get("producto_variante")
        )
        producto_id = to_int(qp.get("producto_id") or qp.get("producto"))
        color_id = to_int(qp.get("color_id") or qp.get("color"))
        talla_id = to_int(qp.get("talla_id") or qp.get("talla"))
        sku_q = (qp.get("sku") or qp.get("q") or "").strip()

        if empresa_id:
            qs = qs.filter(almacen__empresa_id=empresa_id)
        if sucursal_id:
            qs = qs.filter(almacen__sucursal_id=sucursal_id)
        if almacen_id:
            qs = qs.filter(almacen_id=almacen_id)
        if producto_variante_id:
            qs = qs.filter(producto_variante_id=producto_variante_id)
        if producto_id:
            qs = qs.filter(
                models.Q(producto_id=producto_id) |
                models.Q(producto_variante__producto_id=producto_id)
            )
        if color_id:
            qs = qs.filter(producto_variante__color_id=color_id)
        if talla_id:
            qs = qs.filter(producto_variante__talla_id=talla_id)
        if sku_q:
            qs = qs.filter(producto_variante__sku__icontains=sku_q)

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

                if tipo == "ENTRADA":
                    new_qty = current + qty
                elif tipo == "SALIDA":
                    new_qty = current - qty
                else:
                    new_qty = qty

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

        return Response(
            {
                "tipo": tipo,
                "almacen_id": almacen.pk,
                "ajuste_id": ajuste_id,
                "movimiento_id": (getattr(ev, "id_evento", None) if ev else None),
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

    def get_queryset(self):
        qs = (
            AuditoriaEvento.objects.filter(modulo="inventarios", tabla="existencias")
            .select_related("empresa", "usuario")
            .order_by("-created_at", "-id_evento")
        )
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

        limit_raw = (qp.get("limit") or "").strip()
        try:
            limit = int(limit_raw) if limit_raw else 200
        except Exception:
            limit = 200
        limit = max(1, min(limit, 2000))
        return qs[:limit]

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
