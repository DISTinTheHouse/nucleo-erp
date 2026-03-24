from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from ventas.models import Cotizacion, CotizacionDetalle, Pedido, PedidoDetalle, PedidoDetalleTalla
from ventas.api.serializers import (
    CotizacionSerializer,
    CotizacionDetalleSerializer,
    PedidoSerializer,
    PedidoDetalleSerializer,
    PedidoDetalleTallaSerializer,
    PedidoDetalleWithTallasSerializer,
    PedidoOnboardingCreateSerializer,
)
from nucleo.models import SerieFolio

class CotizacionViewSet(viewsets.ModelViewSet):
    queryset = Cotizacion.objects.all()
    serializer_class = CotizacionSerializer
    http_method_names = ['get', 'post', 'patch']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        return qs.none()

    def perform_create(self, serializer):
        empresa = self.request.user.empresa 
        serializer.save(empresa=empresa)

class CotizacionDetalleViewSet(viewsets.ModelViewSet):
    queryset = CotizacionDetalle.objects.all()
    serializer_class = CotizacionDetalleSerializer
    http_method_names = ['get', 'post', 'patch']

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(cotizacion__empresa=empresa)
        return qs.none()

class PedidoViewSet(viewsets.ModelViewSet):
    queryset = Pedido.objects.filter(activo=True)
    serializer_class = PedidoSerializer

    def _snapshot_facturacion(self, pedido):
        try:
            cl = pedido.cliente
        except Exception:
            cl = None
        if not cl:
            return
        updates = {}
        if not pedido.cliente_razon_social:
            updates["cliente_razon_social"] = cl.razon_social
        if not pedido.cliente_nombre:
            updates["cliente_nombre"] = cl.nombre
        if not pedido.cliente_rfc:
            updates["cliente_rfc"] = cl.rfc
        if not pedido.cliente_regimen_fiscal_id and getattr(cl, "sat_regimen_fiscal_id", None):
            updates["cliente_regimen_fiscal_id"] = cl.sat_regimen_fiscal_id
        if not pedido.cliente_direccion_fiscal:
            updates["cliente_direccion_fiscal"] = cl.direccion_fiscal
        if not pedido.cliente_colonia:
            updates["cliente_colonia"] = cl.colonia
        if not pedido.cliente_codigo_postal:
            updates["cliente_codigo_postal"] = cl.codigo_postal
        if not pedido.cliente_ciudad:
            updates["cliente_ciudad"] = cl.ciudad
        if not pedido.cliente_estado:
            updates["cliente_estado"] = cl.estado
        if not pedido.cliente_giro_empresarial:
            updates["cliente_giro_empresarial"] = cl.giro_empresarial
        if updates:
            for k, v in updates.items():
                setattr(pedido, k, v)
            pedido.save(update_fields=list(updates.keys()))

    def _asignar_folio(self, pedido, empresa):
        if pedido.folio:
            return

        if pedido.serie_folio_id:
            serie_folio = SerieFolio.objects.select_for_update().filter(
                pk=pedido.serie_folio_id
            ).first()
        else:
            serie_folio = SerieFolio.objects.select_for_update().filter(
                empresa=empresa,
                sucursal=pedido.sucursal,
                tipo_documento__iexact="PEDIDO",
                activo=True,
            ).order_by("id_serie_folio").first()

        if not serie_folio:
            raise ValidationError(
                {"serie_folio": "No hay una Serie/Folio activa configurada para tipo_documento='Pedido' en esta sucursal."}
            )

        try:
            folio_formateado, nuevo_consecutivo, anio_actual = serie_folio.get_siguiente_folio()
        except Exception:
            raise ValidationError({"folio": "No se pudo generar el folio del pedido."})

        serie_folio.folio_actual = nuevo_consecutivo
        serie_folio.ultimo_anio = anio_actual
        serie_folio.save(update_fields=["folio_actual", "ultimo_anio", "updated_at"])

        pedido.serie_folio = serie_folio
        pedido.folio = folio_formateado
        pedido.folio_consecutivo = nuevo_consecutivo
        pedido.save(update_fields=["serie_folio", "folio", "folio_consecutivo"])

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if not getattr(user, "is_superuser", False) and getattr(user, "empresa", None):
            qs = qs.filter(empresa=user.empresa)
        q = self.request.query_params.get("q") or self.request.query_params.get("folio")
        if q:
            qs = qs.filter(folio__icontains=q)
        return qs

    def perform_create(self, serializer):
        empresa = self.request.user.empresa
        with transaction.atomic():
            pedido = serializer.save(empresa=empresa)
            self._asignar_folio(pedido, empresa)
            self._snapshot_facturacion(pedido)
    
    def perform_destroy(self, instance):
        instance.soft_delete()

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    def onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)

        if request.method.lower() == "get":
            from catalogo.models import Producto, Talla
            from terceros.models import Cliente

            limit = int(request.query_params.get("limit") or 20)
            limit = max(1, min(limit, 50))

            empresa_id = request.query_params.get("empresa_id")
            if getattr(user, "is_superuser", False) and not empresa and empresa_id:
                from nucleo.models import Empresa

                empresa = Empresa.objects.filter(pk=empresa_id).first()

            cliente_q = (request.query_params.get("cliente_q") or "").strip()
            producto_q = (request.query_params.get("producto_q") or "").strip()

            clientes_qs = Cliente.objects.filter(activo=True)
            productos_qs = Producto.objects.filter(activo=True)
            if not getattr(user, "is_superuser", False) and empresa:
                clientes_qs = clientes_qs.filter(empresa=empresa)
                productos_qs = productos_qs.filter(empresa=empresa)
            if getattr(user, "is_superuser", False) and empresa:
                clientes_qs = clientes_qs.filter(empresa=empresa)
                productos_qs = productos_qs.filter(empresa=empresa)

            if cliente_q:
                clientes_qs = clientes_qs.filter(
                    razon_social__icontains=cliente_q
                ) | clientes_qs.filter(
                    nombre__icontains=cliente_q
                ) | clientes_qs.filter(
                    rfc__icontains=cliente_q
                )

            if producto_q:
                productos_qs = productos_qs.filter(nombre__icontains=producto_q)

            clientes = list(
                clientes_qs.order_by("id").values(
                    "id",
                    "razon_social",
                    "nombre",
                    "rfc",
                    "correo",
                    "telefono",
                    "sat_regimen_fiscal_id",
                    "sat_uso_cfdi_id",
                )[:limit]
            )
            productos = list(
                productos_qs.order_by("id").values(
                    "id",
                    "nombre",
                    "descripcion",
                    "precio_base",
                )[:limit]
            )
            tallas = list(Talla.objects.filter(activo=True).order_by("id").values("id", "nombre"))

            data = {
                "vendedor": {
                    "id": getattr(user, "pk", None),
                    "username": getattr(user, "username", None),
                    "email": getattr(user, "email", None),
                    "empresa_id": getattr(empresa, "pk", None),
                },
                "catalogos": {
                    "formas_pago": [{"value": k, "label": v} for k, v in Pedido.FormaPago.choices],
                    "metodos_pago": [{"value": k, "label": v} for k, v in Pedido.MetodoPago.choices],
                    "usos_cfdi": [{"value": k, "label": v} for k, v in Pedido.UsoCfdi.choices],
                    "tipos_pedido": [{"value": k, "label": v} for k, v in Pedido.CHOICES_TIPO_PEDIDO],
                    "tallas": tallas,
                },
                "busqueda": {
                    "clientes": clientes,
                    "productos": productos,
                },
            }
            return Response(data)

        serializer = PedidoOnboardingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        pedido_data = serializer.validated_data["pedido"]
        detalle_data = serializer.validated_data["detalle"]

        from catalogo.models import Producto, Talla

        if not empresa and getattr(user, "is_superuser", False) and pedido_data.get("sucursal"):
            empresa = getattr(pedido_data["sucursal"], "empresa", None)

        if not empresa:
            raise ValidationError({"empresa": "No se pudo resolver la empresa para crear el pedido."})

        def _merge_detalle(rows):
            agrupado = {}
            for row in rows:
                producto_id = row["producto"]
                entry = agrupado.get(producto_id)
                if not entry:
                    entry = {
                        "producto": producto_id,
                        "precio_unitario": row.get("precio_unitario"),
                        "costo_unitario": row.get("costo_unitario"),
                        "tallas": [],
                    }
                    agrupado[producto_id] = entry
                entry["tallas"] += row.get("tallas") or []

            for entry in agrupado.values():
                by_talla = {}
                for t in entry["tallas"]:
                    talla_id = t["talla"]
                    agg = by_talla.get(talla_id)
                    if not agg:
                        by_talla[talla_id] = dict(t)
                        continue
                    agg["cantidad"] = int(agg["cantidad"]) + int(t["cantidad"])
                    agg["lleva_bordado"] = bool(agg.get("lleva_bordado") or t.get("lleva_bordado"))
                    if agg.get("bordado_config") is None and t.get("bordado_config") is not None:
                        agg["bordado_config"] = t.get("bordado_config")
                entry["tallas"] = list(by_talla.values())

            return list(agrupado.values())

        with transaction.atomic():
            pedido = Pedido.objects.create(empresa=empresa, **pedido_data)
            self._asignar_folio(pedido, empresa)
            self._snapshot_facturacion(pedido)

            detalle_data = _merge_detalle(detalle_data)

            for item in detalle_data:
                producto = Producto.objects.filter(
                    pk=item["producto"],
                    activo=True,
                ).first()
                if not getattr(user, "is_superuser", False) and empresa:
                    producto = Producto.objects.filter(
                        pk=item["producto"],
                        empresa=empresa,
                        activo=True,
                    ).first()
                if not producto:
                    raise ValidationError({"detalle": f"Producto inválido: {item['producto']}"})

                precio_unitario = item.get("precio_unitario")
                if precio_unitario is None:
                    precio_unitario = producto.precio_base or 0

                pedido_detalle = PedidoDetalle.objects.create(
                    pedido=pedido,
                    producto=producto,
                    precio_unitario=precio_unitario,
                    costo_unitario=item.get("costo_unitario"),
                    subtotal_linea=0,
                )

                for t in item.get("tallas") or []:
                    talla = Talla.objects.filter(pk=t["talla"], activo=True).first()
                    if not talla:
                        raise ValidationError({"detalle": f"Talla inválida: {t['talla']}"})

                    if t.get("lleva_bordado") and t.get("bordado_config") is None:
                        raise ValidationError({"detalle": "Falta bordado_config en una talla marcada con lleva_bordado=true."})

                    PedidoDetalleTalla.objects.create(
                        pedido_detalle=pedido_detalle,
                        talla=talla,
                        cantidad=t["cantidad"],
                        precio_unitario=precio_unitario,
                        subtotal_talla=0,
                        lleva_bordado=bool(t.get("lleva_bordado")),
                        bordado_config=t.get("bordado_config"),
                    )

            pedido = (
                Pedido.objects.filter(pk=pedido.pk)
                .prefetch_related("detalles__tallas")
                .first()
            )
            return Response(
                {
                    "pedido": PedidoSerializer(pedido).data,
                    "detalles": PedidoDetalleWithTallasSerializer(pedido.detalles.all(), many=True).data,
                },
                status=status.HTTP_201_CREATED,
            )
    
class PedidoDetalleViewSet(viewsets.ModelViewSet):
    queryset = PedidoDetalle.objects.all()
    serializer_class = PedidoDetalleSerializer
    http_method_names = ['get', 'post', 'patch']

    @action(detail=True, methods=['post'])
    def anular(self, request, pk=None):
        return Response({"msg": "PedidoDetalleViewSet.anular"})

class PedidoDetalleTallaViewSet(viewsets.ModelViewSet):
    queryset = PedidoDetalleTalla.objects.all()
    serializer_class = PedidoDetalleTallaSerializer
    http_method_names = ['get', 'post', 'patch']
