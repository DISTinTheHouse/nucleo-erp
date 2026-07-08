import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch, Q, Sum
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
    RecepcionListSerializer,
    RecepcionOnboardingSerializer,
    RecepcionRetrieveSerializer,
    RecepcionSerializer,
)
from inventarios.models import (
    Almacen,
    Existencia,
    MovimientoInventario,
    MovimientoInventarioDetalle,
    Ubicacion,
)
from nucleo.models import Moneda, SerieFolio, Sucursal
from produccion.models import OrdenProduccion, OrdenProduccionDetalle
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
            recepciones_qs = (
                Recepcion.objects.filter(
                    activo=True,
                    tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA,
                )
                .select_related("sucursal", "proveedor", "almacen", "transportista")
                .prefetch_related(
                    Prefetch(
                        "recepciondetalle_set",
                        queryset=RecepcionDetalle.objects.select_related(
                            "producto",
                            "producto_variante",
                            "ubicacion",
                        ).order_by("id"),
                    )
                )
                .order_by("-fecha_recepcion", "-id")
            )
            qs = qs.prefetch_related(
                'ordencompradetalle_set',
                Prefetch("recepcion_set", queryset=recepciones_qs),
            )
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

        porcentaje_iva = Decimal(str(getattr(oc, "porcentaje_iva", 0) or 0))
        total_iva = (subtotal * porcentaje_iva / Decimal("100")).quantize(
            Decimal("0.01")
        )
        gran_total = subtotal + total_iva
        updates = {
            "subtotal": subtotal,
            "gran_total": gran_total,
            "total": subtotal,
            "total_piezas": piezas,
            "impuestos": total_iva,
            "total_iva": total_iva,
            "porcentaje_iva": porcentaje_iva,
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
            has_porcentaje_iva = "porcentaje_iva" in header

            sucursal_id = header.get("sucursal")
            proveedor_id = header.get("proveedor")
            moneda_id = header.get("moneda")
            fecha_oc = header.get("fecha_oc") or timezone.now().date()
            porcentaje_iva = header.get("porcentaje_iva")

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
            if not oc.pk or has_porcentaje_iva:
                oc.porcentaje_iva = Decimal(str(porcentaje_iva or 0))
            if "referencia" in header:
                oc.referencia = header.get("referencia") or None
            if "observaciones" in header:
                oc.observaciones = header.get("observaciones") or None
            oc.folio = None
            if not oc.pk:
                oc.estatus = OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR
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

    def update(self, request, pk=None, *args, **kwargs):
        user = request.user
        empresa = getattr(user, "empresa", None)

        raw = request.data or {}
        serializer = OrdenCompraOnboardingSerializer(data=raw)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        header = data.get("orden_compra") or raw
        detalle = data.get("detalle") or data.get("detalles")

        with transaction.atomic():
            oc = (
                OrdenCompra.objects.select_for_update()
                .filter(pk=pk, empresa=empresa, activo=True)
                .first()
            )
            if not oc:
                return Response(
                    {"detail": "Orden de compra no encontrada."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if oc.estatus in {
                OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
                OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
                OrdenCompra.EstatusOrdenCompra.RECIBIDA,
            }:
                raise ValidationError(
                    {"estatus": "Una orden autorizada o recibida no puede ser modificada."}
                )

            has_sucursal = "sucursal" in header
            has_proveedor = "proveedor" in header
            has_moneda = "moneda" in header
            has_fecha_oc = "fecha_oc" in header
            has_porcentaje_iva = "porcentaje_iva" in header

            sucursal_id = header.get("sucursal")
            proveedor_id = header.get("proveedor")
            moneda_id = header.get("moneda")
            fecha_oc = header.get("fecha_oc")
            porcentaje_iva = header.get("porcentaje_iva")

            oc.usuario = user
            if has_sucursal and sucursal_id:
                oc.sucursal_id = sucursal_id
            if has_proveedor:
                oc.proveedor_id = proveedor_id
            if has_moneda and moneda_id:
                oc.moneda_id = moneda_id
            if has_fecha_oc and fecha_oc:
                oc.fecha_oc = fecha_oc
            if has_porcentaje_iva:
                oc.porcentaje_iva = Decimal(str(porcentaje_iva or 0))
            if "referencia" in header:
                oc.referencia = header.get("referencia") or None
            if "observaciones" in header:
                oc.observaciones = header.get("observaciones") or None

            oc.estatus = OrdenCompra.EstatusOrdenCompra.POR_AUTORIZAR
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

    def destroy(self, request, pk=None, *args, **kwargs):
        user = request.user
        empresa = getattr(user, "empresa", None)

        oc = (
            OrdenCompra.objects.filter(pk=pk, empresa=empresa, activo=True).first()
        )
        if not oc:
            return Response(
                {"detail": "Orden de compra no encontrada."},
                status=status.HTTP_404_NOT_FOUND,
            )

        oc.soft_delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

class RecepcionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Recepcion.objects.all().select_related(
        "orden_compra",
        "op",
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

        # Solo el retrieve individual anida ``detalles``: se traen los renglones
        # con sus FK en un único prefetch (producto/producto_variante/ubicacion)
        # para evitar el N+1 que tendría el shape anidado. El list no lo necesita
        # y se mantiene ligero.
        if self.action == "retrieve":
            qs = qs.prefetch_related(
                Prefetch(
                    "recepciondetalle_set",
                    queryset=RecepcionDetalle.objects.select_related(
                        "producto",
                        "producto_variante",
                        "ubicacion",
                    ).order_by("id"),
                )
            )

        # Filtro opcional por origen (OC / OP). Sigue la convención del proyecto:
        # query param manual validado en get_queryset (no se usa django-filter).
        # Omitirlo devuelve todas las recepciones, exactamente como antes.
        tipo_origen = (self.request.query_params.get("tipo_origen") or "").strip().upper()
        if tipo_origen:
            validos = {
                Recepcion.TipoOrigen.ORDEN_COMPRA,
                Recepcion.TipoOrigen.ORDEN_PRODUCCION,
            }
            if tipo_origen not in validos:
                raise ValidationError(
                    {"tipo_origen": "Valor inválido. Usa OC (orden de compra) u OP (orden de producción)."}
                )
            qs = qs.filter(tipo_origen=tipo_origen)

        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        if getattr(user, "is_superuser", False):
            return qs
        return qs.none()

    def get_serializer_class(self):
        # retrieve → forma anidada dedicada (con ``detalles[]``); list conserva la
        # forma enriquecida plana intacta; el resto (p. ej. la respuesta de
        # onboarding, que instancia RecepcionSerializer directamente) conserva la
        # forma plana original.
        if self.action == "retrieve":
            return RecepcionRetrieveSerializer
        if self.action == "list":
            return RecepcionListSerializer
        return RecepcionSerializer

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

    def _cantidad_recibida_oc(self, orden_compra_detalle_id):
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

    def _cantidad_recibida_op(self, orden_produccion_detalle_id):
        total = (
            RecepcionDetalle.objects.filter(
                orden_produccion_detalle_id=orden_produccion_detalle_id,
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

            cantidad = item["cantidad_recibida"]
            producto = item["producto"]
            producto_variante = item.get("producto_variante")
            producto_id = producto.pk

            existencia = (
                Existencia.objects.select_for_update()
                .filter(
                    producto_id=producto_id,
                    producto_variante_id=getattr(producto_variante, "pk", None),
                    almacen_id=recepcion.almacen_id,
                    ubicacion_id=(ubicacion.pk if ubicacion else None),
                )
                .order_by("id")
                .first()
            )
            if not existencia:
                existencia = Existencia.objects.create(
                    producto=producto,
                    producto_variante=producto_variante,
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
                orden_compra_detalle=item.get("orden_compra_detalle"),
                orden_produccion_detalle=item.get("orden_produccion_detalle"),
                producto=producto,
                producto_variante=producto_variante,
                ubicacion=ubicacion,
                lote_id=item.get("lote"),
                serie_id=item.get("serie"),
                cantidad_recibida=cantidad,
            )

            movimientos.append(
                {
                    "recepcion_detalle_id": detalle.pk,
                    "orden_compra_detalle_id": item.get("orden_compra_detalle").pk if item.get("orden_compra_detalle") else None,
                    "orden_produccion_detalle_id": item.get("orden_produccion_detalle").pk if item.get("orden_produccion_detalle") else None,
                    "producto_id": producto.pk,
                    "producto_variante_id": getattr(producto_variante, "pk", None),
                    "ubicacion_id": existencia.ubicacion_id,
                    "lote_id": detalle.lote_id,
                    "serie_id": detalle.serie_id,
                    "cantidad_before": str(cantidad_antes),
                    "cantidad_after": str(cantidad_despues),
                    "delta": str(cantidad),
                }
            )
        return movimientos

    def _crear_movimiento_formal_recepcion(self, recepcion, movimientos):
        movimiento = MovimientoInventario.objects.create(
            empresa=recepcion.empresa,
            sucursal=recepcion.sucursal,
            pedido_id=None,
            entrega_id=None,
            devolucion_id=None,
            ajuste_inventario_id=None,
            tipo_movimiento="ENTRADA",
            usuario=recepcion.usuario,
            observaciones=recepcion.observaciones,
            recepcion=recepcion,
            transferencia_id=None,
            op_id=recepcion.op_id,
        )

        for item in movimientos:
            MovimientoInventarioDetalle.objects.create(
                movimiento_inventario=movimiento,
                producto_id=item["producto_id"],
                ubicacion_origen_id=None,
                ubicacion_destino_id=item["ubicacion_id"],
                lote_id=item.get("lote_id"),
                serie_id=item.get("serie_id"),
                cantidad=Decimal(str(item["delta"] or 0)),
                costo_unitario=Decimal("0"),
            )

        return movimiento

    def _actualizar_estatus_oc(self, oc):
        detalles = OrdenCompraDetalle.objects.filter(orden_compra=oc).only("id", "cantidad")
        if not detalles.exists():
            return

        total_pendiente = Decimal("0")
        for detalle in detalles:
            ordered = Decimal(str(detalle.cantidad or 0))
            recibido = self._cantidad_recibida_oc(detalle.pk)
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

        op_qs = (
            OrdenProduccion.objects.filter(
                activo=True,
                estatus_op__in=[
                    OrdenProduccion.EstatusOrdenProduccion.PENDIENTE,
                    OrdenProduccion.EstatusOrdenProduccion.PREPARACION,
                    OrdenProduccion.EstatusOrdenProduccion.BORDANDO,
                    OrdenProduccion.EstatusOrdenProduccion.REVISION,
                ],
            )
            .select_related("pedido", "sucursal")
            .prefetch_related("orden_produccion_detalle__producto_variante__producto")
            .order_by("-fecha_inicio", "-op_id")
        )
        if empresa:
            op_qs = op_qs.filter(empresa=empresa)
        if oc_q:
            op_qs = op_qs.filter(
                Q(folio_op__icontains=oc_q)
                | Q(pedido__folio__icontains=oc_q)
                | Q(orden_produccion_detalle__producto_variante__sku__icontains=oc_q)
                | Q(orden_produccion_detalle__producto_variante__producto__nombre__icontains=oc_q)
            ).distinct()

        ordenes_produccion = []
        ops_limitadas = list(op_qs[:50])
        op_detalles_map = {}
        op_ids = [op.pk for op in ops_limitadas]
        if op_ids:
            op_detalles_qs = (
                OrdenProduccionDetalle.objects
                .filter(op_id__in=op_ids, activo=True)
                .select_related("producto_variante__producto")
                .order_by("op_id", "op_detalle_id")
            )
            op_detalles_list = list(op_detalles_qs)

            op_detalle_ids = [d.pk for d in op_detalles_list]
            recibido_op_map = {}
            if op_detalle_ids:
                recibido_op_qs = (
                    RecepcionDetalle.objects.filter(
                        orden_produccion_detalle_id__in=op_detalle_ids,
                        recepcion__activo=True,
                    )
                    .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
                    .values("orden_produccion_detalle_id")
                    .annotate(total=Sum("cantidad_recibida"))
                )
                for row in recibido_op_qs:
                    recibido_op_map[row["orden_produccion_detalle_id"]] = Decimal(str(row["total"] or 0))

            for op_detalle in op_detalles_list:
                producto_variante = op_detalle.producto_variante
                producto = getattr(producto_variante, "producto", None)
                cantidad = Decimal(str(op_detalle.cantidad or 0))
                recibido = recibido_op_map.get(op_detalle.pk, Decimal("0"))
                pendiente = cantidad - recibido
                item = {
                    "id": op_detalle.pk,
                    "producto_id": getattr(producto, "pk", None),
                    "producto_variante_id": getattr(producto_variante, "pk", None),
                    "producto_nombre": (
                        getattr(producto_variante, "nombre", None)
                        or getattr(producto, "nombre", None)
                    ),
                    "cantidad_ordenada": str(cantidad),
                    "cantidad_recibida": str(recibido),
                    "cantidad_pendiente": str(max(pendiente, Decimal("0"))),
                    "descripcion": op_detalle.observaciones,
                }
                op_detalles_map.setdefault(op_detalle.op_id, []).append(item)

        for op in ops_limitadas:
            detalle = []
            for item in op_detalles_map.get(op.pk, []):
                detalle.append(item)
            if not any(Decimal(item["cantidad_pendiente"]) > 0 for item in detalle):
                continue

            ordenes_produccion.append(
                {
                    "id": op.pk,
                    "folio": op.folio_op,
                    "estatus": op.estatus_op,
                    "pedido_id": op.pedido_id,
                    "sucursal_id": op.sucursal_id,
                    "fecha_inicio": op.fecha_inicio,
                    "cerrar_orden": op.cerrar_orden,
                    "detalle": detalle,
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
            "busqueda": {
                "ordenes_compra": ordenes,
                "ordenes_produccion": ordenes_produccion,
            },
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
                orden_compra_id = header.get("orden_compra")
                orden_produccion_id = header.get("orden_produccion")
                if bool(orden_compra_id) == bool(orden_produccion_id):
                    raise ValidationError(
                        {"recepcion": "Debes enviar exactamente uno de orden_compra u orden_produccion."}
                    )

                oc = None
                op = None
                detalles_oc = {}
                detalles_op = {}
                detalle_payload = []

                if orden_compra_id:
                    oc = (
                        OrdenCompra.objects.select_for_update(of=("self",))
                        .select_related("empresa", "sucursal", "proveedor")
                        .filter(pk=orden_compra_id, activo=True)
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
                else:
                    op = (
                        OrdenProduccion.objects.select_for_update()
                        .select_related("empresa", "sucursal")
                        .filter(pk=orden_produccion_id, activo=True)
                        .first()
                    )
                    if not op:
                        raise ValidationError({"orden_produccion": "Orden de producción no encontrada."})
                    if empresa and op.empresa_id != empresa.pk:
                        raise ValidationError({"orden_produccion": "No tienes acceso a esta orden de producción."})
                    if op.estatus_op not in {
                        OrdenProduccion.EstatusOrdenProduccion.PENDIENTE,
                        OrdenProduccion.EstatusOrdenProduccion.PREPARACION,
                        OrdenProduccion.EstatusOrdenProduccion.BORDANDO,
                        OrdenProduccion.EstatusOrdenProduccion.REVISION,
                    }:
                        raise ValidationError({"estatus": "La orden de producción no está disponible para recepción."})

                almacen = (
                    Almacen.objects.select_related("empresa", "sucursal")
                    .filter(pk=header["almacen"])
                    .first()
                )
                if not almacen:
                    raise ValidationError({"almacen": "Almacén no encontrado."})
                empresa_origen = oc.empresa if oc else op.empresa
                sucursal_origen = oc.sucursal if oc else op.sucursal
                proveedor_origen = oc.proveedor if oc else None
                if empresa_origen.pk and almacen.empresa_id and empresa_origen.pk != almacen.empresa_id:
                    raise ValidationError({"almacen": "El almacén no pertenece a la empresa de la orden."})
                if sucursal_origen.pk and almacen.sucursal_id and sucursal_origen.pk != almacen.sucursal_id:
                    raise ValidationError({"almacen": "El almacén no pertenece a la sucursal de la orden."})
                transportista_id = header.get("transportista")
                if transportista_id and not Transportista.objects.filter(pk=transportista_id).exists():
                    raise ValidationError({"transportista": "Transportista no encontrado."})

                if oc:
                    detalles_oc = {
                        d.pk: d
                        for d in OrdenCompraDetalle.objects.select_related("producto")
                        .filter(orden_compra=oc)
                        .order_by("id")
                    }
                    if not detalles_oc:
                        raise ValidationError({"detalle": "La orden de compra no tiene productos para recibir."})

                    for idx, item in enumerate(detalle_raw):
                        detalle_id = item.get("orden_compra_detalle")
                        if not detalle_id:
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} requiere orden_compra_detalle."}
                            )
                        if item.get("orden_produccion_detalle"):
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} no debe enviar orden_produccion_detalle para una OC."}
                            )

                        oc_detalle = detalles_oc.get(detalle_id)
                        if not oc_detalle:
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} no pertenece a la orden de compra."}
                            )

                        cantidad = Decimal(str(item["cantidad_recibida"]))
                        if cantidad <= 0:
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} debe tener cantidad_recibida > 0."}
                            )

                        recibido = self._cantidad_recibida_oc(oc_detalle.pk)
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
                                "orden_compra_detalle": oc_detalle,
                                "orden_produccion_detalle": None,
                                "producto": oc_detalle.producto,
                                "producto_variante": None,
                                "cantidad_recibida": cantidad,
                                "ubicacion": item.get("ubicacion"),
                                "lote": item.get("lote"),
                                "serie": item.get("serie"),
                            }
                        )
                else:
                    detalles_op = {
                        d.pk: d
                        for d in OrdenProduccionDetalle.objects.select_related("producto_variante__producto")
                        .filter(op=op, activo=True)
                        .order_by("op_detalle_id")
                    }
                    if not detalles_op:
                        raise ValidationError({"detalle": "La orden de producción no tiene productos para recibir."})

                    for idx, item in enumerate(detalle_raw):
                        detalle_id = item.get("orden_produccion_detalle")
                        if not detalle_id:
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} requiere orden_produccion_detalle."}
                            )
                        if item.get("orden_compra_detalle"):
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} no debe enviar orden_compra_detalle para una OP."}
                            )

                        op_detalle = detalles_op.get(detalle_id)
                        if not op_detalle:
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} no pertenece a la orden de producción."}
                            )

                        producto_variante = op_detalle.producto_variante
                        producto = getattr(producto_variante, "producto", None)
                        if not producto_variante or not producto:
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} no tiene producto terminado configurado."}
                            )

                        cantidad = Decimal(str(item["cantidad_recibida"]))
                        if cantidad <= 0:
                            raise ValidationError(
                                {"detalle": f"El renglón #{idx + 1} debe tener cantidad_recibida > 0."}
                            )

                        recibido = self._cantidad_recibida_op(op_detalle.pk)
                        ordered = Decimal(str(op_detalle.cantidad or 0))
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
                                "orden_compra_detalle": None,
                                "orden_produccion_detalle": op_detalle,
                                "producto": producto,
                                "producto_variante": producto_variante,
                                "cantidad_recibida": cantidad,
                                "ubicacion": item.get("ubicacion"),
                                "lote": item.get("lote"),
                                "serie": item.get("serie"),
                            }
                        )

                recepcion = Recepcion(
                    tipo_origen=(
                        Recepcion.TipoOrigen.ORDEN_COMPRA
                        if oc
                        else Recepcion.TipoOrigen.ORDEN_PRODUCCION
                    ),
                    orden_compra=oc,
                    op=op,
                    empresa=empresa_origen,
                    sucursal=sucursal_origen,
                    proveedor=proveedor_origen,
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
                movimiento_formal = self._crear_movimiento_formal_recepcion(recepcion, movimientos)

                orden_completa = True
                detalles_origen = detalles_oc.values() if oc else detalles_op.values()
                for detalle in detalles_origen:
                    ordered = Decimal(str(detalle.cantidad or 0))
                    recibido = (
                        self._cantidad_recibida_oc(detalle.pk)
                        if oc
                        else self._cantidad_recibida_op(detalle.pk)
                    )
                    if recibido < ordered:
                        orden_completa = False
                        break

                recepcion.estatus = (
                    Recepcion.EstatusRecepcion.RECIBIDA
                    if orden_completa
                    else Recepcion.EstatusRecepcion.PARCIAL
                )
                recepcion.save(update_fields=["estatus", "updated_at"])
                if oc:
                    self._actualizar_estatus_oc(oc)
                elif orden_completa and op and op.cerrar_orden:
                    op.estatus_op = OrdenProduccion.EstatusOrdenProduccion.COMPLETADO
                    op.fecha_fin = op.fecha_fin or timezone.now()
                    op.save(update_fields=["estatus_op", "fecha_fin"])

                ip = request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR")
                ua = request.META.get("HTTP_USER_AGENT")
                ev = AuditoriaEvento.objects.create(
                    empresa=empresa_origen,
                    usuario=user if getattr(user, "pk", None) else None,
                    modulo="inventarios",
                    accion="ENTRADA",
                    tabla="existencias",
                    id_registro=str(almacen.pk),
                    antes_json={
                        "items": movimientos,
                        "recepcion_id": recepcion.pk,
                        "tipo_origen": recepcion.tipo_origen,
                        "orden_compra_id": recepcion.orden_compra_id,
                        "op_id": recepcion.op_id,
                    },
                    despues_json={
                        "almacen_id": almacen.pk,
                        "sucursal_id": almacen.sucursal_id,
                        "empresa_id": almacen.empresa_id,
                        "recepcion_id": recepcion.pk,
                        "tipo_origen": recepcion.tipo_origen,
                        "orden_compra_id": recepcion.orden_compra_id,
                        "op_id": recepcion.op_id,
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
                    "movimiento_inventario_id": movimiento_formal.pk,
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
