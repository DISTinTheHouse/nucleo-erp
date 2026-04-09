from django.db import transaction
from django.db.models import OuterRef, Prefetch, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from datetime import timedelta
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from django.conf import settings
from ventas.models import (
    Cotizacion,
    CotizacionDetalle,
    CotizacionDetalleTalla,
    CotizacionServicioExtra,
    Pedido,
    PedidoDetalle,
    PedidoDetalleTalla,
    PedidoServicioExtra,
)
from ventas.api.serializers import (
    CotizacionSerializer,
    CotizacionDashboardItemSerializer,
    CotizacionDetalleSerializer,
    CotizacionDetalleWithTallasSerializer,
    CotizacionFullSerializer,
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

    def get_serializer_class(self):
        if getattr(self, "action", None) == "list":
            return CotizacionDashboardItemSerializer
        if getattr(self, "action", None) == "retrieve":
            return CotizacionFullSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        user = self.request.user
        qs = (
            super()
            .get_queryset()
            .select_related("cliente", "sucursal", "moneda", "vendedor")
        )
        if getattr(user, "is_superuser", False):
            return self._apply_filters(qs)
        empresa = getattr(user, "empresa", None)
        if empresa:
            qs = qs.filter(empresa=empresa)
            if not getattr(user, "is_admin_empresa", False):
                qs = qs.filter(vendedor=user)
            return self._apply_filters(qs)
        return qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        serializer.save(empresa=empresa, vendedor=user)

    def perform_update(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        cotizacion = self.get_object()
        if not getattr(user, "is_superuser", False) and empresa and cotizacion.empresa_id != empresa.id:
            raise ValidationError({"cotizacion": "No tienes acceso a esta cotización."})
        if not getattr(user, "is_superuser", False) and not getattr(user, "is_admin_empresa", False):
            if cotizacion.vendedor_id and cotizacion.vendedor_id != getattr(user, "id", None):
                raise ValidationError({"cotizacion": "No tienes acceso a esta cotización."})
        edit_minutes = int(getattr(settings, "COTIZACION_EDIT_WINDOW_MINUTES", 30))
        edit_minutes = max(1, edit_minutes)

        if cotizacion.estatus == 3 and cotizacion.autorizada_at:
            limite = cotizacion.autorizada_at + timedelta(minutes=edit_minutes)
            if timezone.now() > limite and not getattr(user, "is_superuser", False) and not getattr(user, "is_admin_empresa", False):
                raise ValidationError({"cotizacion": "La cotización ya no está dentro del periodo permitido para edición."})
            cotizacion.estatus = 5
            cotizacion.cambios_solicitados_at = timezone.now()

        serializer.save()
        if cotizacion.estatus == 5:
            cotizacion.save(update_fields=["estatus", "cambios_solicitados_at"])

    def _apply_filters(self, qs):
        if getattr(self, "action", None) == "retrieve":
            detalles_qs = CotizacionDetalle.objects.select_related("producto").prefetch_related(
                Prefetch("tallas", queryset=CotizacionDetalleTalla.objects.select_related("talla"))
            )
            qs = qs.prefetch_related(Prefetch("cotizaciondetalle", queryset=detalles_qs))

        if getattr(self, "action", None) == "list":
            pedido_qs = Pedido.objects.filter(cotizacion_id=OuterRef("pk")).order_by("-id")
            qs = qs.annotate(
                pedido_id=Subquery(pedido_qs.values("id")[:1]),
                pedido_folio=Subquery(pedido_qs.values("folio")[:1]),
                piezas=Coalesce(Sum("cotizaciondetalle__tallas__cantidad"), 0),
            )

        estatus = self.request.query_params.get("estatus")
        if estatus:
            try:
                estatus_list = [int(x) for x in str(estatus).split(",") if str(x).strip()]
                qs = qs.filter(estatus__in=estatus_list)
            except Exception:
                raise ValidationError({"estatus": "Filtro inválido. Usa números separados por coma."})

        q = (self.request.query_params.get("q") or "").strip()
        if q:
            q_filter = (
                Q(oc__icontains=q)
                | Q(cliente__razon_social__icontains=q)
                | Q(cliente__nombre__icontains=q)
                | Q(cliente__rfc__icontains=q)
            )
            if q.isdigit():
                q_filter = q_filter | Q(id=int(q))
            qs = qs.filter(q_filter)

        ordering = (self.request.query_params.get("ordering") or "-created_at").strip()
        allowed = {"id", "created_at", "updated_at", "gran_total", "estatus"}
        ordering_fields = []
        for part in ordering.split(","):
            part = part.strip()
            if not part:
                continue
            desc = part.startswith("-")
            field = part[1:] if desc else part
            if field in allowed:
                ordering_fields.append(part)
        if ordering_fields:
            qs = qs.order_by(*ordering_fields)
        else:
            qs = qs.order_by("-created_at")

        return qs

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

    def _snapshot_cotizacion(self, cotizacion_obj):
        detalles_qs = (
            CotizacionDetalle.objects.filter(cotizacion=cotizacion_obj)
            .prefetch_related("tallas")
            .order_by("id")
        )
        servicios_extras_qs = CotizacionServicioExtra.objects.filter(cotizacion=cotizacion_obj).order_by("id")
        return {
            "cotizacion": CotizacionSerializer(cotizacion_obj).data,
            "detalles": CotizacionDetalleWithTallasSerializer(detalles_qs, many=True).data,
            "servicios_extras": list(servicios_extras_qs.values("nombre", "monto", "visible_en_factura")),
        }

    @action(detail=False, methods=["get", "post"], url_path="onboarding")
    def onboarding(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)

        if request.method.lower() == "get":
            from catalogo.models import Producto, Talla
            from terceros.models import Cliente
            from nucleo.models import SatRegimenFiscal

            limit_raw = request.query_params.get("limit")
            limit = None
            if limit_raw not in (None, "", "all", "ALL", "0", "-1"):
                try:
                    limit = int(limit_raw)
                except Exception:
                    limit = 20
                limit = max(1, min(limit, 1000))

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
            if not getattr(user, "is_superuser", False) and not getattr(user, "is_admin_empresa", False):
                clientes_qs = clientes_qs.filter(vendedores__id=getattr(user, "id", None))

            if cliente_q:
                clientes_qs = clientes_qs.filter(
                    razon_social__icontains=cliente_q
                ) | clientes_qs.filter(
                    nombre__icontains=cliente_q
                ) | clientes_qs.filter(
                    rfc__icontains=cliente_q
                )

            if producto_q:
                from decimal import Decimal
                qtxt = producto_q
                cond = Q(nombre__icontains=qtxt) | Q(descripcion__icontains=qtxt)
                if qtxt.isdigit():
                    try:
                        cond = cond | Q(pk=int(qtxt))
                    except Exception:
                        pass
                    try:
                        cond = cond | Q(precio_base=Decimal(qtxt))
                    except Exception:
                        pass
                else:
                    try:
                        cond = cond | Q(precio_base=Decimal(qtxt))
                    except Exception:
                        pass
                productos_qs = productos_qs.filter(cond)

            clientes_qs = clientes_qs.order_by("id").values(
                "id",
                "razon_social",
                "nombre",
                "rfc",
                "correo",
                "telefono",
                "sat_regimen_fiscal_id",
                "sat_uso_cfdi_id",
            )
            productos_qs = productos_qs.order_by("id").values(
                "id",
                "nombre",
                "descripcion",
                "precio_base",
            )
            if limit is not None:
                clientes_qs = clientes_qs[:limit]
                productos_qs = productos_qs[:limit]
            clientes = list(clientes_qs)
            productos = list(productos_qs)
            tallas = list(Talla.objects.filter(activo=True).order_by("id").values("id", "nombre"))
            regimenes_fiscales = list(
                SatRegimenFiscal.objects.filter(activo=True).order_by("codigo").values("codigo", "descripcion")
            )
            regimenes_fiscales = [
                {"value": r["codigo"], "label": f"{r['codigo']} - {r['descripcion']}"}
                for r in regimenes_fiscales
            ]
            tipos_pedido = [{"value": tp[0], "label": tp[1]} for tp in Pedido.CHOICES_TIPO_PEDIDO]
            clientes_qs = clientes_qs.order_by("id").values(
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
            )
            if limit is not None:
                clientes_qs = clientes_qs[:limit]
            clientes = list(clientes_qs)

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

        raw = request.data
        if isinstance(raw, dict):
            raw_dict = raw
        else:
            try:
                raw_dict = dict(raw)
            except Exception:
                raw_dict = {}

        if "cotizacion" not in raw_dict:
            if "pedido" in raw_dict:
                cotizacion_payload = raw_dict.get("pedido") or {}
            else:
                cotizacion_payload = {
                    k: v
                    for k, v in raw_dict.items()
                    if k not in {"detalle", "detalles", "cotizacion_id", "servicios_extras"}
                }
            # filtrar solo campos válidos del modelo
            try:
                from ventas.models import Cotizacion as CotModel
                allowed = {f.name for f in CotModel._meta.get_fields() if getattr(f, "concrete", False)}
                cotizacion_payload = {k: v for k, v in cotizacion_payload.items() if k in allowed}
                for k in ["empresa", "vendedor", "estatus", "created_at", "updated_at", "autorizada_at", "cambios_solicitados_at", "aprobado_snapshot"]:
                    cotizacion_payload.pop(k, None)
            except Exception:
                pass
            normalized = {
                "cotizacion_id": raw_dict.get("cotizacion_id") or (cotizacion_payload.get("id") if isinstance(cotizacion_payload, dict) else None),
                "cotizacion": cotizacion_payload,
                "detalle": raw_dict.get("detalle") or raw_dict.get("detalles") or [],
                "servicios_extras": raw_dict.get("servicios_extras") or [],
            }
            if normalized.get("cotizacion_id") in (None, ""):
                normalized.pop("cotizacion_id", None)
        else:
            normalized = raw_dict
            if isinstance(normalized, dict) and normalized.get("cotizacion_id") in (None, ""):
                normalized = dict(normalized)
                normalized.pop("cotizacion_id", None)

        serializer = CotizacionOnboardingCreateSerializer(data=normalized)
        serializer.is_valid(raise_exception=True)
        cotizacion_id = serializer.validated_data.get("cotizacion_id") or (request.data.get("cotizacion") or {}).get("id")
        cotizacion_data = serializer.validated_data["cotizacion"]
        detalle_data = serializer.validated_data["detalle"]
        servicios_extras_data = serializer.validated_data.get("servicios_extras") or []

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
                    agg["dtf"] = bool(agg.get("dtf") or t.get("dtf"))
                    agg["sublimado"] = bool(agg.get("sublimado") or t.get("sublimado"))
                    agg["lleva_serigrafia"] = bool(agg.get("lleva_serigrafia") or t.get("lleva_serigrafia"))
                    agg["revelado"] = bool(agg.get("revelado") or t.get("revelado"))
                entry["tallas"] = list(by_talla.values())

            return list(agrupado.values())

        def _save_cotizacion_detalle(cotizacion_obj, rows):
            CotizacionDetalle.objects.filter(cotizacion=cotizacion_obj).delete()
            rows = _merge_detalle(rows)
            for item in rows:
                producto = Producto.objects.filter(pk=item["producto"], activo=True).first()
                if not getattr(user, "is_superuser", False) and empresa:
                    producto = Producto.objects.filter(pk=item["producto"], empresa=empresa, activo=True).first()
                if not producto:
                    raise ValidationError({"detalle": f"Producto inválido: {item['producto']}"})

                precio_unitario = item.get("precio_unitario")
                if precio_unitario is None:
                    precio_unitario = producto.precio_base or 0

                cot_det = CotizacionDetalle.objects.create(
                    cotizacion=cotizacion_obj,
                    producto=producto,
                    precio_lista=producto.precio_base or 0,
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
                    CotizacionDetalleTalla.objects.create(
                        cotizacion_detalle=cot_det,
                        talla=talla,
                        cantidad=t["cantidad"],
                        precio_unitario=precio_unitario,
                        subtotal_talla=0,
                        lleva_bordado=bool(t.get("lleva_bordado")),
                        bordado_config=t.get("bordado_config"),
                        dtf=bool(t.get("dtf")),
                        sublimado=bool(t.get("sublimado")),
                        lleva_serigrafia=bool(t.get("lleva_serigrafia")),
                        revelado=bool(t.get("revelado")),
                    )

        def _save_servicios_extras(cotizacion_obj, rows):
            CotizacionServicioExtra.objects.filter(cotizacion=cotizacion_obj).delete()
            for row in rows or []:
                CotizacionServicioExtra.objects.create(
                    cotizacion=cotizacion_obj,
                    nombre=row.get("nombre") or "",
                    monto=row.get("monto") or 0,
                    visible_en_factura=bool(row.get("visible_en_factura", True)),
                )

        with transaction.atomic():
            edit_minutes = int(getattr(settings, "COTIZACION_EDIT_WINDOW_MINUTES", 30))
            edit_minutes = max(1, edit_minutes)
            now = timezone.now()

            try:
                if cotizacion_id:
                    cotizacion = Cotizacion.objects.select_for_update().filter(pk=cotizacion_id).first()
                    if not cotizacion:
                        raise ValidationError({"cotizacion_id": "Cotización no encontrada."})
                    if not getattr(user, "is_superuser", False) and empresa and cotizacion.empresa_id != empresa.id:
                        raise ValidationError({"cotizacion_id": "No tienes acceso a esta cotización."})
                    if not getattr(user, "is_superuser", False) and not getattr(user, "is_admin_empresa", False):
                        if cotizacion.vendedor_id and cotizacion.vendedor_id != getattr(user, "id", None):
                            raise ValidationError({"cotizacion_id": "No tienes acceso a esta cotización."})

                    if cotizacion.estatus == 3 and cotizacion.autorizada_at:
                        limite = cotizacion.autorizada_at + timedelta(minutes=edit_minutes)
                        if now > limite and not getattr(user, "is_superuser", False) and not getattr(user, "is_admin_empresa", False):
                            raise ValidationError({"cotizacion_id": "La cotización ya no está dentro del periodo permitido para edición."})
                        cotizacion.estatus = 5
                        cotizacion.cambios_solicitados_at = now
                    elif cotizacion.estatus == 4:
                        cotizacion.estatus = 2

                    for k, v in cotizacion_data.items():
                        setattr(cotizacion, k, v)
                    update_fields = list(cotizacion_data.keys())
                    update_fields += ["estatus", "cambios_solicitados_at"]
                    cotizacion.save(update_fields=list(dict.fromkeys(update_fields)))
                else:
                    cotizacion = Cotizacion.objects.create(empresa=empresa, vendedor=user, estatus=2, **cotizacion_data)
            except TypeError as e:
                raise ValidationError({"cotizacion": f"Datos inválidos: {str(e)}"})
            except ValueError as e:
                raise ValidationError({"cotizacion": f"Datos inválidos: {str(e)}"})

            _save_cotizacion_detalle(cotizacion, detalle_data)
            _save_servicios_extras(cotizacion, servicios_extras_data)
            cotizacion = Cotizacion.objects.filter(pk=cotizacion.pk).first()
            detalles_qs = CotizacionDetalle.objects.filter(cotizacion=cotizacion).prefetch_related("tallas")
            servicios_extras_qs = CotizacionServicioExtra.objects.filter(cotizacion=cotizacion).order_by("id")
            return Response(
                {
                    "cotizacion": CotizacionSerializer(cotizacion).data,
                    "detalles": CotizacionDetalleWithTallasSerializer(detalles_qs, many=True).data,
                    "servicios_extras": list(
                        servicios_extras_qs.values("id", "nombre", "monto", "visible_en_factura")
                    ),
                    "pedido": None,
                },
                status=status.HTTP_201_CREATED,
            )

    def _require_mesa_control(self, user):
        if getattr(user, "is_superuser", False):
            return
        if getattr(user, "is_admin_empresa", False):
            return
        raise ValidationError({"permiso": "Acción disponible solo para mesa de control."})

    def _copiar_cotizacion_a_pedido(self, cotizacion, empresa):
        pedido = Pedido.objects.create(
            empresa=empresa,
            sucursal=cotizacion.sucursal,
            cliente=cotizacion.cliente,
            cotizacion=cotizacion,
            moneda=cotizacion.moneda,
            tipo_pedido=1,
            estatus=3,
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
            destinatario=cotizacion.destinatario,
            empresa_envio=cotizacion.empresa_envio,
            telefono_envio=cotizacion.telefono_envio,
            celular_envio=cotizacion.celular_envio,
            direccion_envio=cotizacion.direccion_envio,
            colonia_envio=cotizacion.colonia_envio,
            codigo_postal=cotizacion.codigo_postal,
            ciudad_envio=cotizacion.ciudad_envio,
            estado_envio=cotizacion.estado_envio,
            referencias=cotizacion.referencias,
            envio=cotizacion.envio,
            programa_bordados=cotizacion.programa_bordados,
            bordado_pantalones_extras=cotizacion.bordado_pantalones_extras,
            bordado_logotipo=cotizacion.bordado_logotipo,
            serigrafia=cotizacion.serigrafia,
            reflejante=cotizacion.reflejante,
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
        self._asignar_folio_pedido(pedido, empresa)
        self._snapshot_facturacion_pedido(pedido)

        detalles = CotizacionDetalle.objects.filter(cotizacion=cotizacion).prefetch_related("tallas").order_by("id")
        for det in detalles:
            pedido_det = PedidoDetalle.objects.create(
                pedido=pedido,
                producto=det.producto,
                precio_lista=det.precio_lista,
                precio_unitario=det.precio_unitario,
                costo_unitario=det.costo_unitario,
                subtotal_linea=det.subtotal_linea,
            )
            for t in det.tallas.all():
                PedidoDetalleTalla.objects.create(
                    pedido_detalle=pedido_det,
                    talla=t.talla,
                    cantidad=t.cantidad,
                    precio_unitario=t.precio_unitario,
                    subtotal_talla=t.subtotal_talla,
                    lleva_bordado=t.lleva_bordado,
                    bordado_config=t.bordado_config,
                    dtf=t.dtf,
                    sublimado=t.sublimado,
                    lleva_serigrafia=t.lleva_serigrafia,
                    revelado=t.revelado,
                )
        for s in CotizacionServicioExtra.objects.filter(cotizacion=cotizacion).order_by("id"):
            PedidoServicioExtra.objects.create(
                pedido=pedido,
                nombre=s.nombre,
                monto=s.monto,
                visible_en_factura=s.visible_en_factura,
            )
        return pedido

    def _aplicar_cotizacion_a_pedido(self, cotizacion, pedido):
        pedido.empresa = cotizacion.empresa
        pedido.sucursal = cotizacion.sucursal
        pedido.cliente = cotizacion.cliente
        pedido.moneda = cotizacion.moneda
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
        pedido.destinatario = cotizacion.destinatario
        pedido.empresa_envio = cotizacion.empresa_envio
        pedido.telefono_envio = cotizacion.telefono_envio
        pedido.celular_envio = cotizacion.celular_envio
        pedido.direccion_envio = cotizacion.direccion_envio
        pedido.colonia_envio = cotizacion.colonia_envio
        pedido.codigo_postal = cotizacion.codigo_postal
        pedido.ciudad_envio = cotizacion.ciudad_envio
        pedido.estado_envio = cotizacion.estado_envio
        pedido.referencias = cotizacion.referencias
        pedido.envio = cotizacion.envio
        pedido.programa_bordados = cotizacion.programa_bordados
        pedido.bordado_pantalones_extras = cotizacion.bordado_pantalones_extras
        pedido.bordado_logotipo = cotizacion.bordado_logotipo
        pedido.serigrafia = cotizacion.serigrafia
        pedido.reflejante = cotizacion.reflejante
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
        PedidoServicioExtra.objects.filter(pedido=pedido).delete()
        detalles = CotizacionDetalle.objects.filter(cotizacion=cotizacion).prefetch_related("tallas").order_by("id")
        for det in detalles:
            pedido_det = PedidoDetalle.objects.create(
                pedido=pedido,
                producto=det.producto,
                precio_lista=det.precio_lista,
                precio_unitario=det.precio_unitario,
                costo_unitario=det.costo_unitario,
                subtotal_linea=det.subtotal_linea,
            )
            for t in det.tallas.all():
                PedidoDetalleTalla.objects.create(
                    pedido_detalle=pedido_det,
                    talla=t.talla,
                    cantidad=t.cantidad,
                    precio_unitario=t.precio_unitario,
                    subtotal_talla=t.subtotal_talla,
                    lleva_bordado=t.lleva_bordado,
                    bordado_config=t.bordado_config,
                    dtf=t.dtf,
                    sublimado=t.sublimado,
                    lleva_serigrafia=t.lleva_serigrafia,
                    revelado=t.revelado,
                )
        for s in CotizacionServicioExtra.objects.filter(cotizacion=cotizacion).order_by("id"):
            PedidoServicioExtra.objects.create(
                pedido=pedido,
                nombre=s.nombre,
                monto=s.monto,
                visible_en_factura=s.visible_en_factura,
            )

    @action(detail=True, methods=["post"], url_path="autorizar")
    def autorizar(self, request, pk=None):
        user = request.user
        self._require_mesa_control(user)
        cotizacion = self.get_object()
        empresa = cotizacion.empresa

        with transaction.atomic():
            cotizacion = Cotizacion.objects.select_for_update().filter(pk=cotizacion.pk).first()
            if cotizacion.estatus == 4:
                raise ValidationError({"cotizacion": "La cotización está rechazada."})
            pedido_existente = Pedido.objects.select_for_update().filter(cotizacion=cotizacion, activo=True).order_by("-id").first()
            if pedido_existente:
                raise ValidationError({"cotizacion": "La cotización ya tiene un pedido generado."})

            pedido = self._copiar_cotizacion_a_pedido(cotizacion, empresa)
            cotizacion.estatus = 3
            cotizacion.autorizada_at = timezone.now()
            cotizacion.cambios_solicitados_at = None
            cotizacion.aprobado_snapshot = self._snapshot_cotizacion(cotizacion)
            cotizacion.save(update_fields=["estatus", "autorizada_at", "cambios_solicitados_at", "aprobado_snapshot", "updated_at"])

        pedido = Pedido.objects.filter(pk=pedido.pk).prefetch_related("detalles__tallas").first()
        return Response(
            {"cotizacion": CotizacionSerializer(cotizacion).data, "pedido": PedidoSerializer(pedido).data},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="rechazar")
    def rechazar(self, request, pk=None):
        user = request.user
        self._require_mesa_control(user)
        cotizacion = self.get_object()
        with transaction.atomic():
            cotizacion = Cotizacion.objects.select_for_update().filter(pk=cotizacion.pk).first()
            if cotizacion.estatus == 3:
                raise ValidationError({"cotizacion": "La cotización ya está autorizada."})
            cotizacion.estatus = 4
            cotizacion.save(update_fields=["estatus", "updated_at"])
        return Response({"cotizacion": CotizacionSerializer(cotizacion).data})

    @action(detail=True, methods=["post"], url_path="aceptar-cambios")
    def aceptar_cambios(self, request, pk=None):
        user = request.user
        self._require_mesa_control(user)
        cotizacion = self.get_object()
        with transaction.atomic():
            cotizacion = Cotizacion.objects.select_for_update().filter(pk=cotizacion.pk).first()
            if cotizacion.estatus != 5:
                raise ValidationError({"cotizacion": "La cotización no tiene cambios pendientes."})
            pedido = Pedido.objects.select_for_update().filter(cotizacion=cotizacion, activo=True).order_by("-id").first()
            if not pedido:
                raise ValidationError({"cotizacion": "No existe pedido para aplicar cambios."})
            self._aplicar_cotizacion_a_pedido(cotizacion, pedido)
            cotizacion.estatus = 3
            cotizacion.cambios_solicitados_at = None
            cotizacion.aprobado_snapshot = self._snapshot_cotizacion(cotizacion)
            cotizacion.save(update_fields=["estatus", "cambios_solicitados_at", "aprobado_snapshot", "updated_at"])
        return Response({"cotizacion": CotizacionSerializer(cotizacion).data, "pedido_id": pedido.id})

    @action(detail=True, methods=["post"], url_path="rechazar-cambios")
    def rechazar_cambios(self, request, pk=None):
        user = request.user
        self._require_mesa_control(user)
        cotizacion = self.get_object()
        with transaction.atomic():
            cotizacion = Cotizacion.objects.select_for_update().filter(pk=cotizacion.pk).first()
            if cotizacion.estatus != 5:
                raise ValidationError({"cotizacion": "La cotización no tiene cambios pendientes."})
            snapshot = cotizacion.aprobado_snapshot or {}
            snap_cot = snapshot.get("cotizacion") or {}
            snap_det = snapshot.get("detalles") or []
            snap_servicios = snapshot.get("servicios_extras") or []
            if not snap_cot:
                raise ValidationError({"cotizacion": "No hay snapshot aprobado para revertir."})

            skip = {"id", "empresa", "empresa_id", "created_at", "updated_at", "aprobado_snapshot"}
            model_fields = {f.name for f in Cotizacion._meta.fields}
            for k, v in snap_cot.items():
                if k in skip:
                    continue
                if k in model_fields:
                    setattr(cotizacion, k, v)
            cotizacion.estatus = 3
            cotizacion.cambios_solicitados_at = None
            cotizacion.save(update_fields=["estatus", "cambios_solicitados_at", "updated_at"] + [k for k in snap_cot.keys() if k in model_fields and k not in skip])

            CotizacionDetalle.objects.filter(cotizacion=cotizacion).delete()
            CotizacionServicioExtra.objects.filter(cotizacion=cotizacion).delete()
            for det in snap_det:
                prod_id = det.get("producto")
                if isinstance(prod_id, dict):
                    prod_id = prod_id.get("id")
                cot_det = CotizacionDetalle.objects.create(
                    cotizacion=cotizacion,
                    producto_id=prod_id,
                    precio_lista=det.get("precio_lista"),
                    precio_unitario=det.get("precio_unitario") or 0,
                    costo_unitario=det.get("costo_unitario"),
                    subtotal_linea=det.get("subtotal_linea") or 0,
                )
                for t in det.get("tallas") or []:
                    CotizacionDetalleTalla.objects.create(
                        cotizacion_detalle=cot_det,
                        talla_id=t.get("talla"),
                        cantidad=t.get("cantidad") or 1,
                        precio_unitario=t.get("precio_unitario"),
                        subtotal_talla=t.get("subtotal_talla") or 0,
                        lleva_bordado=bool(t.get("lleva_bordado")),
                        bordado_config=t.get("bordado_config"),
                        dtf=bool(t.get("dtf")),
                        sublimado=bool(t.get("sublimado")),
                        lleva_serigrafia=bool(t.get("lleva_serigrafia")),
                        revelado=bool(t.get("revelado")),
                    )
            for s in snap_servicios:
                CotizacionServicioExtra.objects.create(
                    cotizacion=cotizacion,
                    nombre=s.get("nombre") or "",
                    monto=s.get("monto") or 0,
                    visible_en_factura=bool(s.get("visible_en_factura", True)),
                )
        return Response({"cotizacion": CotizacionSerializer(cotizacion).data})

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
