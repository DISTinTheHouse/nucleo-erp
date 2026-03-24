from django.db import transaction
from datetime import timedelta
from django.utils import timezone
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
    CotizacionOnboardingCreateSerializer,
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

    def perform_update(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        cotizacion = self.get_object()
        if not getattr(user, "is_superuser", False) and empresa and cotizacion.empresa_id != empresa.id:
            raise ValidationError({"cotizacion": "No tienes acceso a esta cotización."})

        pedido = Pedido.objects.filter(cotizacion=cotizacion, activo=True).order_by("-id").first()
        if pedido and getattr(pedido, "created_at", None):
            limite = pedido.created_at + timedelta(minutes=30)
            if timezone.now() > limite and not getattr(user, "is_superuser", False) and not getattr(user, "is_admin_empresa", False):
                raise ValidationError({"cotizacion": "La cotización ya no está dentro del periodo permitido para edición."})

        serializer.save()
        pedido = Pedido.objects.filter(cotizacion=cotizacion, activo=True).order_by("-id").first()
        if pedido:
            pedido.estatus = 2
            pedido.save(update_fields=["estatus"])

    def _asignar_folio_pedido(self, pedido, empresa):
        if pedido.folio:
            return
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

    def _snapshot_facturacion_pedido(self, pedido):
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

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    def onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)

        if request.method.lower() == "get":
            from catalogo.models import Producto, Talla
            from terceros.models import Cliente
            from nucleo.models import SatRegimenFiscal

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
            regimenes_fiscales = list(
                SatRegimenFiscal.objects.filter(activo=True).order_by("codigo").values("codigo", "descripcion")
            )
            regimenes_fiscales = [
                {"value": r["codigo"], "label": f"{r['codigo']} - {r['descripcion']}"}
                for r in regimenes_fiscales
            ]
            tipos_pedido = [{"value": tp[0], "label": tp[1]} for tp in Pedido.CHOICES_TIPO_PEDIDO]
            clientes = list(
                clientes_qs.order_by("id").values(
                    "id",
                    "razon_social",
                    "nombre",
                    "rfc",
                    "correo",
                    "telefono",
                    "direccion_fiscal",
                    "colonia",
                    "codigo_postal",
                    "ciudad",
                    "estado",
                    "giro_empresarial",
                    "sat_regimen_fiscal_id",
                    "sat_regimen_fiscal__codigo",
                    "sat_regimen_fiscal__descripcion",
                )[:limit]
            )

            data = {
                "vendedor": {
                    "id": getattr(user, "pk", None),
                    "username": getattr(user, "username", None),
                    "email": getattr(user, "email", None),
                    "empresa_id": getattr(empresa, "pk", None),
                },
                "catalogos": {
                    "formas_pago": [{"value": k, "label": v} for k, v in Cotizacion.FormaPago.choices],
                    "metodos_pago": [{"value": k, "label": v} for k, v in Cotizacion.MetodoPago.choices],
                    "usos_cfdi": [{"value": k, "label": v} for k, v in Cotizacion.UsoCfdi.choices],
                    "tallas": tallas,
                    "tipos_pedido": tipos_pedido,
                    "regimenes_fiscales": regimenes_fiscales,
                },
                "busqueda": {
                    "clientes": clientes,
                    "productos": productos,
                },
            }
            return Response(data)

        serializer = CotizacionOnboardingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cotizacion_id = serializer.validated_data.get("cotizacion_id") or (request.data.get("cotizacion") or {}).get("id")
        cotizacion_data = serializer.validated_data["cotizacion"]
        detalle_data = serializer.validated_data["detalle"]

        from catalogo.models import Producto, Talla

        if not getattr(user, "is_superuser", False) and not empresa:
            raise ValidationError({"empresa": "El usuario no tiene empresa asignada."})

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
            if cotizacion_id:
                cotizacion = Cotizacion.objects.select_for_update().filter(pk=cotizacion_id).first()
                if not cotizacion:
                    raise ValidationError({"cotizacion_id": "Cotización no encontrada."})
                if not getattr(user, "is_superuser", False) and empresa and cotizacion.empresa_id != empresa.id:
                    raise ValidationError({"cotizacion_id": "No tienes acceso a esta cotización."})

                pedido = Pedido.objects.select_for_update().filter(cotizacion=cotizacion, activo=True).order_by("-id").first()
                if pedido and getattr(pedido, "created_at", None):
                    limite = pedido.created_at + timedelta(minutes=30)
                    if timezone.now() > limite and not getattr(user, "is_superuser", False) and not getattr(user, "is_admin_empresa", False):
                        raise ValidationError({"cotizacion_id": "La cotización ya no está dentro del periodo permitido para edición."})

                for k, v in cotizacion_data.items():
                    setattr(cotizacion, k, v)
                cotizacion.save(update_fields=list(cotizacion_data.keys()))

                if not pedido:
                    pedido = Pedido.objects.create(
                        empresa=cotizacion.empresa,
                        sucursal=cotizacion.sucursal,
                        cliente=cotizacion.cliente,
                        cotizacion=cotizacion,
                        moneda=cotizacion.moneda,
                        tipo_pedido=1,
                        estatus=2,
                        persona_pagos=cotizacion.persona_pagos,
                        correo_facturas=cotizacion.correo_facturas,
                        telefono_pagos=cotizacion.telefono_pagos,
                        forma_pago=cotizacion.forma_pago,
                        metodo_pago=cotizacion.metodo_pago,
                        uso_cfdi=cotizacion.uso_cfdi,
                    )
                else:
                    pedido.empresa = cotizacion.empresa
                    pedido.sucursal = cotizacion.sucursal
                    pedido.cliente = cotizacion.cliente
                    pedido.moneda = cotizacion.moneda
                    pedido.estatus = 2
                    pedido.recompra = cotizacion.recompra
                    pedido.chat_online = cotizacion.chat_online
                    pedido.pedido_online = cotizacion.pedido_online
                    pedido.prospeccion = cotizacion.prospeccion
                    pedido.recomendacion = cotizacion.recomendacion
                    pedido.amazon = cotizacion.amazon
                    pedido.google = cotizacion.google
                    pedido.publicidad = cotizacion.publicidad
                    pedido.mercado_libre = cotizacion.mercado_libre
                    pedido.redes_sociales = cotizacion.redes_sociales
                    pedido.otro = cotizacion.otro
                    pedido.mailing = cotizacion.mailing
                    pedido.persona_pagos = cotizacion.persona_pagos
                    pedido.correo_facturas = cotizacion.correo_facturas
                    pedido.telefono_pagos = cotizacion.telefono_pagos
                    pedido.oc = cotizacion.oc
                    pedido.forma_pago = cotizacion.forma_pago
                    pedido.metodo_pago = cotizacion.metodo_pago
                    pedido.uso_cfdi = cotizacion.uso_cfdi
                    pedido.anticipo_total = cotizacion.anticipo_total
                    pedido.anticipo_parcial = cotizacion.anticipo_parcial
                    pedido.vendedor_autoriza = cotizacion.vendedor_autoriza
                    pedido.pago_antes_embarque = cotizacion.pago_antes_embarque
                    pedido.por_confirmar = cotizacion.por_confirmar
                    pedido.otra_cantidad = cotizacion.otra_cantidad
                    pedido.monto = cotizacion.monto
                    pedido.empaque_ecologico = cotizacion.empaque_ecologico
                    pedido.embarque_parcial = cotizacion.embarque_parcial
                    pedido.comentarios_parcialidad = cotizacion.comentarios_parcialidad
                    pedido.envio = cotizacion.envio
                    pedido.programa_bordados = cotizacion.programa_bordados
                    pedido.bordado_pantalones_extras = cotizacion.bordado_pantalones_extras
                    pedido.bordado_logotipo = cotizacion.bordado_logotipo
                    pedido.observaciones = cotizacion.observaciones
                    pedido.flete = cotizacion.flete
                    pedido.seguros = cotizacion.seguros
                    pedido.anticipo = cotizacion.anticipo
                    pedido.subtotal = cotizacion.subtotal
                    pedido.descuento_global = cotizacion.descuento_global
                    pedido.ieps = cotizacion.ieps
                    pedido.iva = cotizacion.iva
                    pedido.gran_total = cotizacion.gran_total
                    pedido.save()

                PedidoDetalle.objects.filter(pedido=pedido).delete()
            else:
                cotizacion = Cotizacion.objects.create(empresa=empresa, **cotizacion_data)

                pedido = Pedido.objects.create(
                    empresa=empresa,
                    sucursal=cotizacion.sucursal,
                    cliente=cotizacion.cliente,
                    cotizacion=cotizacion,
                    moneda=cotizacion.moneda,
                    tipo_pedido=1,
                    estatus=2,
                    recompra=cotizacion.recompra,
                    chat_online=cotizacion.chat_online,
                    pedido_online=cotizacion.pedido_online,
                    prospeccion=cotizacion.prospeccion,
                    recomendacion=cotizacion.recomendacion,
                    amazon=cotizacion.amazon,
                    google=cotizacion.google,
                    publicidad=cotizacion.publicidad,
                    mercado_libre=cotizacion.mercado_libre,
                    redes_sociales=cotizacion.redes_sociales,
                    otro=cotizacion.otro,
                    mailing=cotizacion.mailing,
                    persona_pagos=cotizacion.persona_pagos,
                    correo_facturas=cotizacion.correo_facturas,
                    telefono_pagos=cotizacion.telefono_pagos,
                    oc=cotizacion.oc,
                    forma_pago=cotizacion.forma_pago,
                    metodo_pago=cotizacion.metodo_pago,
                    uso_cfdi=cotizacion.uso_cfdi,
                    anticipo_total=cotizacion.anticipo_total,
                    anticipo_parcial=cotizacion.anticipo_parcial,
                    vendedor_autoriza=cotizacion.vendedor_autoriza,
                    pago_antes_embarque=cotizacion.pago_antes_embarque,
                    por_confirmar=cotizacion.por_confirmar,
                    otra_cantidad=cotizacion.otra_cantidad,
                    monto=cotizacion.monto,
                    empaque_ecologico=cotizacion.empaque_ecologico,
                    embarque_parcial=cotizacion.embarque_parcial,
                    comentarios_parcialidad=cotizacion.comentarios_parcialidad,
                    envio=cotizacion.envio,
                    programa_bordados=cotizacion.programa_bordados,
                    bordado_pantalones_extras=cotizacion.bordado_pantalones_extras,
                    bordado_logotipo=cotizacion.bordado_logotipo,
                    observaciones=cotizacion.observaciones,
                    flete=cotizacion.flete,
                    seguros=cotizacion.seguros,
                    anticipo=cotizacion.anticipo,
                    subtotal=cotizacion.subtotal,
                    descuento_global=cotizacion.descuento_global,
                    ieps=cotizacion.ieps,
                    iva=cotizacion.iva,
                    gran_total=cotizacion.gran_total,
                )

            self._asignar_folio_pedido(pedido, cotizacion.empresa)
            self._snapshot_facturacion_pedido(pedido)

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
                    "cotizacion": CotizacionSerializer(cotizacion).data,
                    "pedido": PedidoSerializer(pedido).data,
                    "detalles": PedidoDetalleWithTallasSerializer(pedido.detalles.all(), many=True).data,
                },
                status=status.HTTP_201_CREATED,
            )

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
