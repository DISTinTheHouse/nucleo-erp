import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from auditoria.models import AuditoriaEvento
from catalogo.models import Producto
from compras.models import OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
from compras.api.serializers import (
    OrdenCompraOnboardingSerializer,
    OrdenCompraRetrieveSerializer,
    OrdenCompraSerializer,
    OrdenCompraDetalleSerializer,
    RecepcionDetalleSerializer,
    RecepcionOnboardingSerializer,
    RecepcionSerializer,
)
from inventarios.models import Almacen, Existencia, Ubicacion
from nucleo.models import Moneda, SerieFolio, Sucursal
from terceros.models import Proveedor, Transportista

logger = logging.getLogger(__name__)

class OrdenCompraViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OrdenCompra.objects.filter(activo=True)
    serializer_class = OrdenCompraSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset().select_related('proveedor')
        empresa = getattr(user, "empresa", None)
        if empresa:
            qs = qs.filter(empresa=empresa)
        else:
            return qs.none()
        if self.action == 'retrieve':
            qs = qs.prefetch_related('ordencompradetalle_set')
        return qs

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return OrdenCompraRetrieveSerializer
        return OrdenCompraSerializer

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

            has_sucursal = "sucursal" in header
            has_proveedor = "proveedor" in header
            has_moneda = "moneda" in header
            has_fecha_oc = "fecha_oc" in header

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
            if not oc.pk or has_sucursal:
                oc.sucursal_id = sucursal_id
            if not oc.pk or has_proveedor:
                oc.proveedor_id = proveedor_id
            if not oc.pk or has_moneda:
                oc.moneda_id = moneda_id
            if not oc.pk or has_fecha_oc:
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
        body_proveedor_id = request.data.get("proveedor") or request.data.get("proveedor_id")
        try:
            body_proveedor_id = int(body_proveedor_id) if body_proveedor_id not in (None, "") else None
        except Exception:
            body_proveedor_id = None

        if OrdenCompraDetalle.objects.filter(orden_compra=oc).count() <= 0:
            raise ValidationError({"detalle": "Agrega al menos un producto antes de aceptar."})

        with transaction.atomic():
            oc = OrdenCompra.objects.select_for_update().filter(pk=oc.pk).first()
            if body_proveedor_id:
                oc.proveedor_id = body_proveedor_id
            if not oc.proveedor_id:
                raise ValidationError({"proveedor": "Proveedor es requerido para aceptar."})
            if not oc.folio:
                self._asignar_folio_oc(oc, oc.empresa)
            oc.estatus = OrdenCompra.EstatusOrdenCompra.AUTORIZADA
            oc.fecha_autorizacion = timezone.now()
            oc.save(update_fields=["proveedor", "estatus", "fecha_autorizacion", "updated_at"])

        return Response({"orden_compra": OrdenCompraSerializer(oc).data}, status=status.HTTP_200_OK)

class RecepcionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Recepcion.objects.all().select_related(
        "orden_compra",
        "empresa",
        "sucursal",
        "proveedor",
        "almacen",
        "transportista",
        "usuario",
    )
    serializer_class = RecepcionSerializer
    http_method_names = ["get", "post"]

    def get_queryset(self):
        user = self.request.user
        qs = self.queryset.filter(activo=True).order_by("-fecha_recepcion", "-id")
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        if getattr(user, "is_superuser", False):
            return qs
        return qs.none()

    def _serie_folio_recepcion(self, empresa, sucursal, serie_codigo):
        qs = SerieFolio.objects.select_for_update().filter(
            empresa=empresa,
            sucursal=sucursal,
            activo=True,
        )
        serie_codigo = (serie_codigo or "").strip().upper()
        series_validas = ["RC", "RT", "RZ"]
        if serie_codigo:
            serie_especifica = qs.filter(
                Q(tipo_documento__iexact="RECEPCION", serie__iexact=serie_codigo)
                | Q(tipo_documento__iexact=serie_codigo)
            )
            serie_folio = serie_especifica.order_by("id_serie_folio").first()
            if serie_folio:
                return serie_folio

        qs = qs.filter(
            Q(tipo_documento__iexact="RECEPCION", serie__in=series_validas)
            | Q(tipo_documento__in=series_validas)
        )
        return qs.order_by("id_serie_folio").first()

    def _asignar_folio_recepcion(self, recepcion, serie_codigo):
        serie_folio = self._serie_folio_recepcion(
            recepcion.empresa, recepcion.sucursal, serie_codigo
        )
        if not serie_folio:
            codigo = (serie_codigo or "RC/RT/RZ").upper()
            raise ValidationError(
                {
                    "serie_codigo": (
                        "No hay una Serie/Folio activa configurada para recepción "
                        f"con serie {codigo} ni una alternativa disponible entre RC, RT o RZ."
                    )
                }
            )

        try:
            folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
        except Exception:
            raise ValidationError({"folio": "No se pudo generar el folio de la recepción."})

        serie_folio.folio_actual = nuevo_consecutivo
        serie_folio.ultimo_anio = anio_actual
        serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])
        recepcion.folio = folio_formateado

    def _cantidad_recibida(self, orden_compra_detalle_id):
        total = (
            RecepcionDetalle.objects.filter(
                orden_compra_detalle_id=orden_compra_detalle_id,
                recepcion__activo=True,
            )
            .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .aggregate(total=Sum("cantidad_recibida"))
            .get("total")
        )
        return Decimal(str(total or 0))

    def _actualizar_existencias(self, recepcion, detalle_payload):
        movimientos = []
        for item in detalle_payload:
            ubicacion = None
            ubicacion_id = item.get("ubicacion")
            if recepcion.almacen.requiere_ubicacion and not ubicacion_id:
                raise ValidationError(
                    {"ubicacion": "La ubicación es requerida para este almacén."}
                )
            if ubicacion_id:
                ubicacion = Ubicacion.objects.filter(
                    pk=ubicacion_id,
                    almacen_id=recepcion.almacen_id,
                ).first()
                if not ubicacion:
                    raise ValidationError(
                        {"ubicacion": "La ubicación no pertenece al almacén seleccionado."}
                    )

            oc_detalle = item["oc_detalle"]
            cantidad = item["cantidad_recibida"]
            producto_id = oc_detalle.producto_id

            existencia = (
                Existencia.objects.select_for_update()
                .filter(
                    producto_id=producto_id,
                    almacen_id=recepcion.almacen_id,
                    ubicacion_id=(ubicacion.pk if ubicacion else None),
                )
                .order_by("id")
                .first()
            )
            if not existencia:
                existencia = Existencia.objects.create(
                    producto_id=producto_id,
                    almacen=recepcion.almacen,
                    ubicacion=ubicacion,
                    stock=0,
                    cantidad=Decimal("0"),
                )

            cantidad_antes = existencia.cantidad or Decimal("0")
            cantidad_despues = cantidad_antes + cantidad
            existencia.cantidad = cantidad_despues
            try:
                existencia.stock = int(cantidad_despues)
            except Exception:
                existencia.stock = existencia.stock or 0
            existencia.save(update_fields=["cantidad", "stock", "fecha_actualizacion"])

            detalle = RecepcionDetalle.objects.create(
                recepcion=recepcion,
                orden_compra_detalle=oc_detalle,
                producto=oc_detalle.producto,
                ubicacion=ubicacion,
                lote_id=item.get("lote"),
                serie_id=item.get("serie"),
                cantidad_recibida=cantidad,
            )

            movimientos.append(
                {
                    "recepcion_detalle_id": detalle.pk,
                    "orden_compra_detalle_id": oc_detalle.pk,
                    "producto_id": oc_detalle.producto_id,
                    "ubicacion_id": existencia.ubicacion_id,
                    "cantidad_before": str(cantidad_antes),
                    "cantidad_after": str(cantidad_despues),
                    "delta": str(cantidad),
                }
            )
        return movimientos

    def _actualizar_estatus_oc(self, oc):
        detalles = OrdenCompraDetalle.objects.filter(orden_compra=oc).only("id", "cantidad")
        if not detalles.exists():
            return

        total_pendiente = Decimal("0")
        for detalle in detalles:
            ordered = Decimal(str(detalle.cantidad or 0))
            recibido = self._cantidad_recibida(detalle.pk)
            pendiente = ordered - recibido
            if pendiente > 0:
                total_pendiente += pendiente

        if total_pendiente > 0:
            oc.estatus = OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA
        else:
            oc.estatus = OrdenCompra.EstatusOrdenCompra.RECIBIDA
        oc.save(update_fields=["estatus", "updated_at"])

    def handle_get_onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)
        if not empresa and not getattr(user, "is_superuser", False):
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

        oc_q = (request.query_params.get("q") or "").strip()
        almacen_id = request.query_params.get("almacen_id")

        ordenes_qs = (
            OrdenCompra.objects.filter(
                activo=True,
                estatus__in=[
                    OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
                    OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
                ],
            )
            .select_related("proveedor", "sucursal")
            .order_by("-updated_at", "-id")
        )
        if empresa:
            ordenes_qs = ordenes_qs.filter(empresa=empresa)
        if oc_q:
            ordenes_qs = ordenes_qs.filter(
                Q(folio__icontains=oc_q)
                | Q(referencia__icontains=oc_q)
                | Q(proveedor__nombre__icontains=oc_q)
                | Q(proveedor__razon_social__icontains=oc_q)
            )

        ordenes_limitadas = list(ordenes_qs[:50])
        # ── Bulk-fetch detalle for all OCs (avoid N+1) ──
        oc_ids = [oc.pk for oc in ordenes_limitadas]
        oc_detalles_map = {}  # oc_id → [detalle_dict, …]
        if oc_ids:
            detalles_qs = (
                OrdenCompraDetalle.objects
                .filter(orden_compra_id__in=oc_ids)
                .select_related("producto")
                .order_by("orden_compra_id", "id")
            )
            detalles_list = list(detalles_qs)

            detalle_ids = [d.pk for d in detalles_list]
            recibido_map = {}
            if detalle_ids:
                recibido_qs = (
                    RecepcionDetalle.objects.filter(
                        orden_compra_detalle_id__in=detalle_ids,
                        recepcion__activo=True,
                    )
                    .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
                    .values("orden_compra_detalle_id")
                    .annotate(total=Sum("cantidad_recibida"))
                )
                for row in recibido_qs:
                    recibido_map[row["orden_compra_detalle_id"]] = Decimal(str(row["total"] or 0))

            for detalle in detalles_list:
                recibido = recibido_map.get(detalle.pk, Decimal("0"))
                ordered = Decimal(str(detalle.cantidad or 0))
                pendiente = ordered - recibido
                item = {
                    "id": detalle.pk,
                    "producto_id": detalle.producto_id,
                    "producto_nombre": detalle.producto.nombre,
                    "cantidad_ordenada": str(ordered),
                    "cantidad_recibida": str(recibido),
                    "cantidad_pendiente": str(max(pendiente, Decimal("0"))),
                    "descripcion": detalle.descripcion,
                }
                oc_detalles_map.setdefault(detalle.orden_compra_id, []).append(item)

        # ── Build ordenes with embedded detalle ──
        ordenes = []
        for oc in ordenes_limitadas:
            ordenes.append(
                {
                    "id": oc.pk,
                    "folio": oc.folio,
                    "estatus": oc.estatus,
                    "proveedor_id": oc.proveedor_id,
                    "proveedor_nombre": getattr(oc.proveedor, "nombre", None),
                    "sucursal_id": oc.sucursal_id,
                    "fecha_oc": oc.fecha_oc,
                    "detalle": oc_detalles_map.get(oc.pk, []),
                }
            )

        almacenes_qs = Almacen.objects.filter(estatus="ACTIVO")
        if empresa:
            almacenes_qs = almacenes_qs.filter(empresa=empresa)
        almacenes = list(
            almacenes_qs.order_by("codigo").values(
                "id_almacen",
                "codigo",
                "nombre",
                "sucursal_id",
                "requiere_ubicacion",
            )
        )

        ubicaciones_qs = Ubicacion.objects.filter(estatus="ACTIVO")
        if almacen_id:
            ubicaciones_qs = ubicaciones_qs.filter(almacen_id=almacen_id)
        else:
            ubicaciones_qs = ubicaciones_qs.none()
        ubicaciones = list(
            ubicaciones_qs.order_by("pasillo", "rack", "nivel", "posicion").values(
                "id_ubicacion",
                "almacen_id",
                "pasillo",
                "rack",
                "nivel",
                "posicion",
            )
        )

        series_qs = SerieFolio.objects.filter(activo=True)
        if empresa:
            series_qs = series_qs.filter(empresa=empresa)
        series_qs = series_qs.filter(
            Q(tipo_documento__iexact="RECEPCION", serie__in=["RC", "RT", "RZ"])
            | Q(tipo_documento__in=["RC", "RT", "RZ"])
        )
        series = list(
            series_qs.order_by("serie", "tipo_documento")
            .values("id_serie_folio", "tipo_documento", "serie", "sucursal_id")[:50]
        )

        return {
            "user": {"id": getattr(user, "pk", None), "email": getattr(user, "email", None)},
            "empresa_id": getattr(empresa, "pk", None),
            "catalogos": {
                "almacenes": almacenes,
                "ubicaciones": ubicaciones,
                "series_recepcion": series,
            },
            "busqueda": {"ordenes_compra": ordenes},
        }

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    def onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)

        if request.method.lower() == "get":
            return Response(self.handle_get_onboarding(request))

        try:
            serializer = RecepcionOnboardingSerializer(data=request.data or {})
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            header = data["recepcion"]
            detalle_raw = data["detalle"]
            serie_codigo = (header.get("serie_codigo") or "RC").strip().upper()

            if not empresa and not getattr(user, "is_superuser", False):
                raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})
            if serie_codigo not in {"RC", "RT", "RZ"}:
                raise ValidationError({"serie_codigo": "serie_codigo debe ser RC, RT o RZ."})

            with transaction.atomic():
                oc = (
                    OrdenCompra.objects.select_for_update(of=("self",))
                    .select_related("empresa", "sucursal", "proveedor")
                    .filter(pk=header["orden_compra"], activo=True)
                    .first()
                )
                if not oc:
                    raise ValidationError({"orden_compra": "Orden de compra no encontrada."})
                if empresa and oc.empresa_id != empresa.pk:
                    raise ValidationError({"orden_compra": "No tienes acceso a esta orden de compra."})
                if oc.estatus not in {
                    OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
                    OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
                }:
                    raise ValidationError({"estatus": "La orden de compra no está disponible para recepción."})
                if not oc.proveedor_id:
                    raise ValidationError({"proveedor": "La orden de compra no tiene proveedor asignado."})

                almacen = (
                    Almacen.objects.select_related("empresa", "sucursal")
                    .filter(pk=header["almacen"])
                    .first()
                )
                if not almacen:
                    raise ValidationError({"almacen": "Almacén no encontrado."})
                if oc.empresa_id and almacen.empresa_id and oc.empresa_id != almacen.empresa_id:
                    raise ValidationError({"almacen": "El almacén no pertenece a la empresa de la orden."})
                if oc.sucursal_id and almacen.sucursal_id and oc.sucursal_id != almacen.sucursal_id:
                    raise ValidationError({"almacen": "El almacén no pertenece a la sucursal de la orden."})
                transportista_id = header.get("transportista")
                if transportista_id and not Transportista.objects.filter(pk=transportista_id).exists():
                    raise ValidationError({"transportista": "Transportista no encontrado."})

                detalles_oc = {
                    d.pk: d
                    for d in OrdenCompraDetalle.objects.select_related("producto")
                    .filter(orden_compra=oc)
                    .order_by("id")
                }
                if not detalles_oc:
                    raise ValidationError({"detalle": "La orden de compra no tiene productos para recibir."})

                detalle_payload = []
                for idx, item in enumerate(detalle_raw):
                    oc_detalle = detalles_oc.get(item["orden_compra_detalle"])
                    if not oc_detalle:
                        raise ValidationError(
                            {"detalle": f"El renglón #{idx + 1} no pertenece a la orden de compra."}
                        )

                    cantidad = Decimal(str(item["cantidad_recibida"]))
                    if cantidad <= 0:
                        raise ValidationError(
                            {"detalle": f"El renglón #{idx + 1} debe tener cantidad_recibida > 0."}
                        )

                    recibido = self._cantidad_recibida(oc_detalle.pk)
                    ordered = Decimal(str(oc_detalle.cantidad or 0))
                    pendiente = ordered - recibido
                    if pendiente <= 0:
                        raise ValidationError(
                            {
                                "detalle": (
                                    f"El producto del renglón #{idx + 1} ya fue recibido completamente."
                                )
                            }
                        )
                    if cantidad > pendiente:
                        raise ValidationError(
                            {
                                "detalle": (
                                    f"El renglón #{idx + 1} excede la cantidad pendiente por recibir."
                                )
                            }
                        )
                    detalle_payload.append(
                        {
                            "oc_detalle": oc_detalle,
                            "cantidad_recibida": cantidad,
                            "ubicacion": item.get("ubicacion"),
                            "lote": item.get("lote"),
                            "serie": item.get("serie"),
                        }
                    )

                recepcion = Recepcion(
                    orden_compra=oc,
                    empresa=oc.empresa,
                    sucursal=oc.sucursal,
                    proveedor=oc.proveedor,
                    almacen=almacen,
                    transportista_id=transportista_id,
                    usuario=user,
                    fecha_recepcion=header.get("fecha_recepcion") or timezone.now(),
                    remision=(header.get("remision") or None),
                    factura_referencia=(header.get("factura_referencia") or None),
                    observaciones=(header.get("observaciones") or None),
                    estatus=Recepcion.EstatusRecepcion.BORRADOR,
                    activo=True,
                )
                self._asignar_folio_recepcion(recepcion, serie_codigo)
                recepcion.save()

                movimientos = self._actualizar_existencias(recepcion, detalle_payload)

                orden_completa = True
                for detalle in detalles_oc.values():
                    ordered = Decimal(str(detalle.cantidad or 0))
                    recibido = self._cantidad_recibida(detalle.pk)
                    if recibido < ordered:
                        orden_completa = False
                        break

                recepcion.estatus = (
                    Recepcion.EstatusRecepcion.RECIBIDA
                    if orden_completa
                    else Recepcion.EstatusRecepcion.PARCIAL
                )
                recepcion.save(update_fields=["estatus", "updated_at"])
                self._actualizar_estatus_oc(oc)

                ip = request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR")
                ua = request.META.get("HTTP_USER_AGENT")
                ev = AuditoriaEvento.objects.create(
                    empresa=oc.empresa,
                    usuario=user if getattr(user, "pk", None) else None,
                    modulo="inventarios",
                    accion="ENTRADA",
                    tabla="existencias",
                    id_registro=str(almacen.pk),
                    antes_json={"items": movimientos, "recepcion_id": recepcion.pk},
                    despues_json={
                        "almacen_id": almacen.pk,
                        "sucursal_id": almacen.sucursal_id,
                        "empresa_id": almacen.empresa_id,
                        "recepcion_id": recepcion.pk,
                        "items": movimientos,
                    },
                    ip=ip,
                    user_agent=ua,
                )

            detalles_recepcion = RecepcionDetalle.objects.filter(recepcion=recepcion).order_by("id")
            return Response(
                {
                    "recepcion": RecepcionSerializer(recepcion).data,
                    "detalle": RecepcionDetalleSerializer(detalles_recepcion, many=True).data,
                    "movimiento_id": ev.id_evento,
                },
                status=status.HTTP_200_OK,
            )
        except ValidationError as exc:
            logger.warning(
                "Recepcion onboarding validation error | user=%s | superuser=%s | empresa=%s | body=%s | detail=%s",
                getattr(user, "username", None),
                getattr(user, "is_superuser", False),
                getattr(empresa, "pk", None),
                request.data,
                getattr(exc, "detail", exc),
            )
            raise
