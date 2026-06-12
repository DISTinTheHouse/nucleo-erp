from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from catalogo.models import Producto
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion
from compras.api.serializers import (
    OrdenCompraOnboardingSerializer,
    OrdenCompraSerializer,
    OrdenCompraDetalleSerializer,
    RecepcionDetalleSerializer,
    RecepcionSerializer,
)
from nucleo.models import Moneda, SerieFolio, Sucursal
from terceros.models import Proveedor

class OrdenCompraViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OrdenCompra.objects.filter(activo=True)
    serializer_class = OrdenCompraSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        return qs.none()

    def _asignar_folio_oc(self, instance, empresa):
        serie_folio = SerieFolio.objects.filter(
            empresa=empresa,
            sucursal=instance.sucursal,
            tipo_documento__iexact="ORDEN_COMPRA",
            activo=True,
        ).order_by("id_serie_folio").first()

        if serie_folio:
            try:
                folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
                serie_folio.folio_actual = nuevo_consecutivo
                serie_folio.ultimo_anio = anio_actual
                serie_folio.save(update_fields=["folio_actual", "ultimo_anio"])
                instance.folio = folio_formateado
                instance.save(update_fields=["folio"])
                return
            except Exception:
                pass

        count = OrdenCompra.objects.filter(empresa=empresa).count()
        instance.folio = f"OC-{empresa.pk}-{instance.id or count+1}"
        instance.save(update_fields=["folio"])

    def _get_default_sucursal(self, user, empresa):
        suc = getattr(user, "sucursal_default", None)
        if suc and getattr(suc, "empresa_id", None) == getattr(empresa, "pk", None):
            return suc
        try:
            return Sucursal.objects.filter(empresa=empresa, activo=True).order_by("codigo").first()
        except Exception:
            return None

    def _get_default_moneda(self, empresa):
        moneda = getattr(empresa, "moneda_base", None)
        if moneda:
            return moneda
        m = Moneda.objects.filter(empresa__isnull=True, codigo_iso="MXN", activo=True).first()
        if m:
            return m
        return Moneda.objects.filter(empresa__isnull=True, activo=True).order_by("codigo_iso").first()

    def _recalcular_totales(self, oc: OrdenCompra):
        detalles_qs = OrdenCompraDetalle.objects.filter(orden_compra=oc).only(
            "cantidad", "importe"
        )
        subtotal = Decimal("0")
        piezas = 0
        for d in detalles_qs:
            subtotal += Decimal(str(getattr(d, "importe", 0) or 0))
            piezas += int(getattr(d, "piezas", 0) or getattr(d, "cantidad", 0) or 0)

        updates = {
            "subtotal": subtotal,
            "gran_total": subtotal,
            "total": subtotal,
            "total_piezas": piezas,
            "total_iva": Decimal("0"),
            "porcentaje_iva": Decimal("0"),
        }
        for k, v in updates.items():
            setattr(oc, k, v)
        oc.save(update_fields=list(updates.keys()) + ["updated_at"])

    def handle_get_onboarding(self, request):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        if not empresa and not getattr(user, "is_superuser", False):
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

        limit_raw = request.query_params.get("limit")
        try:
            limit = int(limit_raw) if limit_raw not in (None, "", "all", "0", "-1") else 50
        except Exception:
            limit = 50
        limit = max(1, min(limit, 200))

        proveedor_q = (request.query_params.get("proveedor_q") or "").strip()
        producto_q = (request.query_params.get("producto_q") or "").strip()

        proveedores_qs = Proveedor.objects.filter(activo=True)
        productos_qs = Producto.objects.filter(activo=True)
        if empresa:
            proveedores_qs = proveedores_qs.filter(empresa=empresa)
            productos_qs = productos_qs.filter(empresa=empresa)

        if proveedor_q:
            proveedores_qs = proveedores_qs.filter(
                Q(nombre__icontains=proveedor_q)
                | Q(razon_social__icontains=proveedor_q)
                | Q(rfc__icontains=proveedor_q)
                | Q(codigo__icontains=proveedor_q)
            )
        if producto_q:
            productos_qs = productos_qs.filter(
                Q(nombre__icontains=producto_q) | Q(descripcion__icontains=producto_q)
            )

        proveedores = list(
            proveedores_qs.order_by("nombre").values("id", "codigo", "nombre", "razon_social", "rfc")[:limit]
        )
        productos = list(
            productos_qs.order_by("nombre").values("id", "nombre", "descripcion", "precio_base")[:limit]
        )
        sucursales = []
        if empresa:
            sucursales = list(
                Sucursal.objects.filter(empresa=empresa, activo=True)
                .order_by("codigo")
                .values("id_sucursal", "codigo", "nombre")
            )
        monedas = list(
            Moneda.objects.filter(Q(empresa__isnull=True) | Q(empresa=empresa), activo=True)
            .order_by("codigo_iso")
            .values("id", "codigo_iso", "nombre")
        )
        return {
            "user": {"id": getattr(user, "pk", None), "email": getattr(user, "email", None)},
            "empresa_id": getattr(empresa, "pk", None),
            "catalogos": {
                "sucursales": sucursales,
                "monedas": monedas,
            },
            "busqueda": {
                "proveedores": proveedores,
                "productos": productos,
            },
        }

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    def onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)

        if request.method.lower() == "get":
            return Response(self.handle_get_onboarding(request))

        raw = request.data or {}
        serializer = OrdenCompraOnboardingSerializer(data=raw)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        orden_compra_id = data.get("orden_compra_id")
        header = data.get("orden_compra") or {}
        detalle = data.get("detalle")

        if not empresa and not getattr(user, "is_superuser", False):
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

        with transaction.atomic():
            if orden_compra_id:
                oc = (
                    OrdenCompra.objects.select_for_update()
                    .filter(pk=orden_compra_id, activo=True)
                    .first()
                )
                if not oc:
                    raise ValidationError({"orden_compra_id": "Orden de compra no encontrada."})
                if empresa and oc.empresa_id != empresa.pk:
                    raise ValidationError({"orden_compra_id": "No tienes acceso a esta orden de compra."})
                if oc.estatus not in {
                    OrdenCompra.EstatusOrdenCompra.BORRADOR,
                    OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
                }:
                    raise ValidationError({"estatus": "La orden ya no puede editarse."})
            else:
                oc = OrdenCompra()

            sucursal_id = header.get("sucursal")
            proveedor_id = header.get("proveedor")
            moneda_id = header.get("moneda")
            fecha_oc = header.get("fecha_oc") or timezone.now().date()

            if not sucursal_id:
                suc = self._get_default_sucursal(user, empresa)
                if not suc:
                    raise ValidationError({"sucursal": "Sucursal es requerida."})
                sucursal_id = suc.pk

            if not moneda_id:
                m = self._get_default_moneda(empresa)
                if not m:
                    raise ValidationError({"moneda": "Moneda es requerida."})
                moneda_id = m.pk

            oc.empresa = empresa
            oc.usuario = user
            oc.sucursal_id = sucursal_id
            oc.proveedor_id = proveedor_id
            oc.moneda_id = moneda_id
            oc.fecha_oc = fecha_oc
            if "referencia" in header:
                oc.referencia = header.get("referencia") or None
            if "observaciones" in header:
                oc.observaciones = header.get("observaciones") or None
            oc.folio = None
            if not oc.pk:
                oc.estatus = OrdenCompra.EstatusOrdenCompra.BORRADOR
            oc.save()

            if detalle is not None:
                OrdenCompraDetalle.objects.filter(orden_compra=oc).delete()
                for it in detalle:
                    producto_id = it.get("producto")
                    cantidad = int(it.get("cantidad") or 0)
                    precio = it.get("precio")
                    if precio in (None, ""):
                        precio = Decimal("0")
                    else:
                        precio = Decimal(str(precio))
                    descuento = Decimal("0")
                    importe = (Decimal(cantidad) * precio) - descuento
                    OrdenCompraDetalle.objects.create(
                        orden_compra=oc,
                        producto_id=producto_id,
                        solicitud_compra_detalle=None,
                        requisicion_detalle=None,
                        descripcion=(it.get("descripcion") or None),
                        cantidad=cantidad,
                        precio=precio,
                        descuento=descuento,
                        importe=importe,
                        sucursal_id=oc.sucursal_id,
                        piezas=cantidad,
                    )
                self._recalcular_totales(oc)

        detalles_qs = OrdenCompraDetalle.objects.filter(orden_compra=oc).order_by("id")
        return Response(
            {
                "orden_compra": OrdenCompraSerializer(oc).data,
                "detalle": OrdenCompraDetalleSerializer(detalles_qs, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="aceptar")
    def aceptar(self, request, pk=None):
        user = request.user
        empresa = getattr(user, "empresa", None)
        oc = self.get_object()
        if empresa and oc.empresa_id != empresa.pk:
            raise ValidationError({"orden_compra_id": "No tienes acceso a esta orden de compra."})
        if oc.estatus not in {
            OrdenCompra.EstatusOrdenCompra.BORRADOR,
            OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR,
        }:
            raise ValidationError({"estatus": "La orden ya no puede aceptarse."})
        if not oc.proveedor_id:
            raise ValidationError({"proveedor": "Proveedor es requerido para aceptar."})
        if OrdenCompraDetalle.objects.filter(orden_compra=oc).count() <= 0:
            raise ValidationError({"detalle": "Agrega al menos un producto antes de aceptar."})

        with transaction.atomic():
            oc = OrdenCompra.objects.select_for_update().filter(pk=oc.pk).first()
            if not oc.folio:
                self._asignar_folio_oc(oc, oc.empresa)
            oc.estatus = OrdenCompra.EstatusOrdenCompra.AUTORIZADA
            oc.fecha_autorizacion = timezone.now()
            oc.save(update_fields=["estatus", "fecha_autorizacion", "updated_at"])

        return Response({"orden_compra": OrdenCompraSerializer(oc).data}, status=status.HTTP_200_OK)

class RecepcionViewSet(viewsets.ModelViewSet):
    queryset = Recepcion.objects.all()
    serializer_class = RecepcionSerializer
    http_method_names = ['get', 'post', 'patch']

        
