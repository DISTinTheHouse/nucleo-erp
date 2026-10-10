from datetime import timedelta
from decimal import Decimal

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from django.conf import settings
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from finanzas.services.facturama.acceso import empresa_usa_facturama
from terceros.models import Proveedor, Cliente, DireccionCliente
from terceros.api.serializers import ProveedorSerializer, ClienteSerializer, DireccionClienteSerializer
from terceros.scope import clientes_base, clientes_visibles
import json
import base64
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

class ClienteViewSetMesaControl(viewsets.ModelViewSet):
    queryset = clientes_base()
    serializer_class = ClienteSerializer
    http_method_names = ['get']

    def get_queryset(self):
        # Mismo alcance que ``ClienteViewSet`` y que el buscador global: una sola
        # definición en ``terceros.scope``. Antes reconstruía desde
        # ``Cliente.objects``, con lo que perdía el ``activo=True`` de su propio
        # ``queryset`` de clase y servía clientes borrados; tampoco aplicaba el
        # scope por ``vendedores`` ni la política de superusuario.
        return clientes_visibles(super().get_queryset(), self.request.user)

class ClienteViewSet(viewsets.ModelViewSet):
    queryset = clientes_base()
    serializer_class = ClienteSerializer

    def get_queryset(self):
        # El predicado de aislamiento vive en ``terceros.scope`` para que el
        # buscador global (``/api/v1/search/``) reutilice EXACTAMENTE éste y no
        # una copia que pueda separarse de él.
        return clientes_visibles(super().get_queryset(), self.request.user)

    def retrieve(self, request, *args, **kwargs):
        # Sólo el detalle (no el listado) lleva ``resumen_comercial``: es la
        # vista "como vendedor" de un cliente puntual, y calcularla por fila en
        # el listado dispararía una consulta de agregación por cada cliente.
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = dict(serializer.data)
        data["resumen_comercial"] = self._resumen_comercial(instance)
        return Response(data)

    def _resumen_comercial(self, cliente):
        # Import local: ``ventas`` ya importa ``terceros.models`` a nivel de
        # módulo (FK ``Cliente``), así que importar ``ventas.models`` aquí
        # arriba crearía un ciclo al cargar las apps.
        from ventas.models import Pedido, Cotizacion

        estatus_pedido = dict(Pedido.CHOICES_ESTATUS)
        # Sólo documentos de la empresa del cliente: sin este filtro, un pedido o
        # cotización de otra empresa que apunte a este cliente (#249) aparecía
        # aquí con su folio y montos.
        pedidos_qs = Pedido.objects.filter(
            cliente=cliente, empresa_id=cliente.empresa_id, activo=True
        )

        pedidos_por_estatus = {
            estatus_pedido.get(fila["estatus"], fila["estatus"]): fila["total"]
            for fila in pedidos_qs.values("estatus").annotate(total=Count("id"))
        }
        # Excluye CANCELADO (5) para no inflar el monto histórico con pedidos
        # que nunca se concretaron; se separa por moneda porque un cliente
        # puede tener pedidos en más de una.
        montos_por_moneda = [
            {"moneda": fila["moneda__codigo_iso"], "total": fila["total"]}
            for fila in pedidos_qs.exclude(estatus=5)
            .values("moneda__codigo_iso")
            .annotate(total=Sum("gran_total"))
        ]

        recientes = pedidos_qs.select_related("moneda").order_by("-created_at")[:5].values(
            "id", "folio", "created_at", "estatus", "gran_total", "moneda__codigo_iso"
        )
        pedidos_recientes = [
            {
                "id": fila["id"],
                "folio": fila["folio"],
                "fecha": fila["created_at"],
                "estatus": fila["estatus"],
                "estatus_display": estatus_pedido.get(fila["estatus"], fila["estatus"]),
                "gran_total": fila["gran_total"],
                "moneda": fila["moneda__codigo_iso"],
            }
            for fila in recientes
        ]

        return {
            "total_pedidos": pedidos_qs.count(),
            "total_cotizaciones": Cotizacion.objects.filter(
                cliente=cliente, empresa_id=cliente.empresa_id
            ).count(),
            "pedidos_por_estatus": pedidos_por_estatus,
            "montos_por_moneda": montos_por_moneda,
            "ultimo_pedido": pedidos_recientes[0] if pedidos_recientes else None,
            "pedidos_recientes": pedidos_recientes,
        }

    def perform_create(self, serializer):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        cliente = serializer.save(empresa=empresa)
        try:
            if getattr(user, "id", None):
                cliente.vendedores.add(user)
        except Exception:
            pass
        # La cuenta de Facturama es de UNA empresa: los clientes de las demás no se
        # le suben (antes se mandaban todos, de cualquier tenant).
        if not empresa_usa_facturama(cliente.empresa):
            return
        try:
            base_url = getattr(settings, "FACTURAMA_BASE_URL", "https://apisandbox.facturama.mx").rstrip("/")
            url = f"{base_url}/Client"
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            usern = (getattr(settings, "FACTURAMA_USERNAME", "") or "").strip()
            pwd = (getattr(settings, "FACTURAMA_PASSWORD", "") or "").strip()
            if usern and pwd:
                token = base64.b64encode(f"{usern}:{pwd}".encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
            payload = {
                "Email": (cliente.correo or "").strip(),
                "Rfc": (cliente.rfc or "").strip().upper(),
                "Name": (cliente.razon_social or cliente.nombre or "").strip(),
                "FiscalRegime": getattr(getattr(cliente, "sat_regimen_fiscal", None), "codigo", ""),
                "CfdiUse": getattr(getattr(cliente, "sat_uso_cfdi", None), "codigo", ""),
                "TaxZipCode": (cliente.codigo_postal or "").strip(),
            }
            address = {
                "Street": (cliente.direccion_fiscal or "").strip(),
                "Neighborhood": (cliente.colonia or "").strip(),
                "ZipCode": (cliente.codigo_postal or "").strip(),
                "Municipality": (cliente.ciudad or "").strip(),
                "State": (cliente.estado or "").strip(),
                "Country": "MEXICO",
            }
            address = {k: v for k, v in address.items() if v}
            if address:
                payload["Address"] = address
            payload = {k: v for k, v in payload.items() if v}
            raw_body = json.dumps(payload).encode("utf-8")
            try:
                req = Request(url, data=raw_body, headers=headers, method="POST")
                with urlopen(req, timeout=10) as resp:
                    resp.read()  # ignore body; best-effort
            except (HTTPError, URLError, TimeoutError, ValueError):
                pass
        except Exception:
            pass

    def perform_destroy(self, instance):
        instance.soft_delete()

    # ``detail=False`` fuera de ``get_queryset()`` a propósito (EC-422): mismo
    # criterio que los demás ``kpis`` -- todo con ``aggregate()``/``Sum``/
    # ``Count`` en DB, nunca iterando clientes/facturas/CxC fila por fila.
    @action(detail=False, methods=["get"], url_path="kpis")
    def kpis(self, request):
        user = request.user
        empresa = getattr(user, "empresa", None)
        if empresa is None and not getattr(user, "is_superuser", False):
            return Response(self._kpis_vacio())

        # Mismo alcance que el listado (``clientes_visibles``): un vendedor
        # normal solo ve los KPIs de SUS clientes, Mesa de Control/admin ven
        # toda la empresa. Import local: ver nota en ``_resumen_comercial``.
        clientes_qs = clientes_visibles(clientes_base(), user)
        data = {
            "generado_en": timezone.now(),
            "ventas_por_cliente": self._kpi_ventas_por_cliente(clientes_qs),
            "clientes_activos": self._kpi_clientes_activos(clientes_qs),
            "reclamos_devoluciones": {
                "disponible": False,
                "motivo": "Devolucion/DevolucionDetalle no registran cantidad de piezas (solo los FKs); Entrega tampoco, así que no hay piezas devueltas ni piezas embarcadas que dividir.",
            },
            "cartera_antiguedad": self._kpi_cartera_antiguedad(clientes_qs),
        }
        return Response(data)

    def _kpis_vacio(self):
        motivo = {"disponible": False, "motivo": "Usuario sin empresa asignada."}
        return {
            "generado_en": timezone.now(),
            "ventas_por_cliente": motivo,
            "clientes_activos": motivo,
            "reclamos_devoluciones": motivo,
            "cartera_antiguedad": motivo,
        }

    def _kpi_ventas_por_cliente(self, clientes_qs):
        from finanzas.models import Factura

        filas = list(
            Factura.objects.filter(cliente__in=clientes_qs, activo=True)
            .exclude(estatus=Factura.FacturaStatus.CANCELADA)
            .values("cliente_id", "cliente__nombre")
            .annotate(monto=Sum("total"))
            .order_by("-monto")
        )
        gran_total = sum((f["monto"] or Decimal("0")) for f in filas)

        top_clientes = []
        acumulado = Decimal("0")
        for fila in filas[:5]:
            monto = fila["monto"] or Decimal("0")
            acumulado += monto
            top_clientes.append({
                "cliente_id": fila["cliente_id"],
                "cliente_nombre": fila["cliente__nombre"],
                "monto": monto,
                "pct_del_total": round(float(monto) / float(gran_total) * 100, 1) if gran_total else 0.0,
                "pct_acumulado": round(float(acumulado) / float(gran_total) * 100, 1) if gran_total else 0.0,
            })

        return {
            "disponible": True,
            "total_facturado": gran_total,
            "total_clientes_facturados": len(filas),
            "top_clientes": top_clientes,
        }

    def _kpi_clientes_activos(self, clientes_qs):
        from ventas.models import Pedido

        desde = timezone.now() - timedelta(days=90)
        total = clientes_qs.count()
        activos = (
            Pedido.objects.filter(cliente__in=clientes_qs, activo=True, created_at__gte=desde)
            .values("cliente_id")
            .distinct()
            .count()
        )
        inactivos = max(total - activos, 0)
        return {
            "disponible": True,
            "total": total,
            "activos": activos,
            "inactivos": inactivos,
            "pct_activos": round(activos / total * 100, 1) if total else None,
            "ventana_dias": 90,
        }

    def _kpi_cartera_antiguedad(self, clientes_qs):
        from finanzas.models import CuentaPorCobrar

        hoy = timezone.localdate()
        hace_30 = hoy - timedelta(days=30)
        hace_60 = hoy - timedelta(days=60)
        vencidas = CuentaPorCobrar.objects.filter(
            cliente__in=clientes_qs,
            estatus__in=[CuentaPorCobrar.EstatusCxC.PENDIENTE, CuentaPorCobrar.EstatusCxC.PARCIAL],
            fecha_vencimiento__isnull=False,
            fecha_vencimiento__lt=hoy,
        )
        agg = vencidas.aggregate(
            total_cuentas=Count("id"),
            saldo_total=Sum("saldo"),
            b_0_30_total=Count("id", filter=Q(fecha_vencimiento__gte=hace_30)),
            b_0_30_monto=Sum("saldo", filter=Q(fecha_vencimiento__gte=hace_30)),
            b_31_60_total=Count("id", filter=Q(fecha_vencimiento__lt=hace_30, fecha_vencimiento__gte=hace_60)),
            b_31_60_monto=Sum("saldo", filter=Q(fecha_vencimiento__lt=hace_30, fecha_vencimiento__gte=hace_60)),
            b_60_mas_total=Count("id", filter=Q(fecha_vencimiento__lt=hace_60)),
            b_60_mas_monto=Sum("saldo", filter=Q(fecha_vencimiento__lt=hace_60)),
        )
        drill_down = list(
            vencidas.select_related("cliente")
            .order_by("fecha_vencimiento")
            .values("id", "cliente_id", "cliente__nombre", "saldo", "fecha_vencimiento")[:20]
        )
        for fila in drill_down:
            fila["dias_vencida"] = (hoy - fila["fecha_vencimiento"]).days

        return {
            "disponible": True,
            "saldo_total_vencido": agg["saldo_total"] or Decimal("0"),
            "total_cuentas_vencidas": agg["total_cuentas"] or 0,
            "buckets": {
                "0_30": {"total": agg["b_0_30_total"] or 0, "monto": agg["b_0_30_monto"] or Decimal("0")},
                "31_60": {"total": agg["b_31_60_total"] or 0, "monto": agg["b_31_60_monto"] or Decimal("0")},
                "60_mas": {"total": agg["b_60_mas_total"] or 0, "monto": agg["b_60_mas_monto"] or Decimal("0")},
            },
            "drill_down": drill_down,
        }

class HistorialOrdenesCompraPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def _fecha_param(request, nombre):
    """Fecha ``YYYY-MM-DD`` del query string; mal formada o imposible → 400."""
    valor = (request.query_params.get(nombre) or "").strip()
    if not valor:
        return None
    try:
        fecha = parse_date(valor)
    except ValueError:
        fecha = None
    if fecha is None:
        raise ValidationError({nombre: "Fecha inválida; formato YYYY-MM-DD."})
    return fecha


class ProveedorViewSet(viewsets.ModelViewSet):
    queryset = Proveedor.objects.filter(activo=True)
    serializer_class = ProveedorSerializer

    def get_queryset(self):
        user = self.request.user
        # Listado más reciente primero por fecha de alta del proveedor;
        # ``-id`` como desempate estable.
        qs = super().get_queryset().order_by("-fecha_alta", "-id")
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        return qs.none()

    def perform_create(self, serializer):
        empresa = getattr(self.request.user, "empresa", None)
        if empresa is None:
            raise ValidationError({"empresa": "El usuario no tiene una empresa asignada."})
        serializer.save(empresa=empresa)

    def perform_destroy(self, instance):
        instance.activo = False
        instance.fecha_baja = timezone.localdate()
        instance.save(update_fields=["activo", "fecha_baja"])

    @action(detail=True, methods=["get"], url_path="historial-ordenes-compra")
    def historial_ordenes_compra(self, request, pk=None):
        """OC de este proveedor para su detalle (EC-399). El proveedor se acota
        con ``get_object``; las OC, por la empresa del usuario con el mismo
        criterio que el listado de OC (superusuario incluido)."""
        from compras.models import OrdenCompra
        from compras.api.serializers import OrdenCompraSerializer
        from compras.services.orden_compra_view_service import (
            filtrar_campos_contabilidad_orden_compra,
            puede_ver_contabilidad,
        )

        proveedor = self.get_object()
        empresa = getattr(request.user, "empresa", None)
        qs = (
            OrdenCompra.objects.filter(proveedor=proveedor, empresa=empresa, activo=True)
            .select_related("empresa", "sucursal", "moneda", "usuario", "pedido", "proveedor")
        ) if empresa is not None else OrdenCompra.objects.none()

        estatus = (request.query_params.get("estatus") or "").strip()
        if estatus:
            try:
                estatus = int(estatus)
            except ValueError:
                raise ValidationError({"estatus": "Debe ser un entero válido."})
            if estatus not in OrdenCompra.EstatusOrdenCompra.values:
                raise ValidationError({"estatus": "Estatus de OC inexistente."})
            qs = qs.filter(estatus=estatus)

        fecha_inicio = _fecha_param(request, "fecha_inicio")
        fecha_final = _fecha_param(request, "fecha_final")
        if fecha_inicio and fecha_final and fecha_inicio > fecha_final:
            raise ValidationError({"fecha_inicio": "No puede ser posterior a `fecha_final`."})
        if fecha_inicio:
            qs = qs.filter(fecha_oc__gte=fecha_inicio)
        if fecha_final:
            qs = qs.filter(fecha_oc__lte=fecha_final)

        qs = qs.order_by("-fecha_oc", "-id")

        paginator = HistorialOrdenesCompraPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        ver_montos = puede_ver_contabilidad(request.user)
        data = [
            item if ver_montos else filtrar_campos_contabilidad_orden_compra(item, request.user)
            for item in OrdenCompraSerializer(page, many=True).data
        ]
        response = paginator.get_paginated_response(data)

        resumen = {
            # Mismo conteo que ``count``: lo hizo ya el paginador.
            "total_ordenes": paginator.page.paginator.count,
            "por_estatus": {
                fila["estatus"]: fila["total"]
                for fila in qs.order_by().values("estatus").annotate(total=Count("id"))
            },
        }
        if ver_montos:
            # Excluye CANCELADA: una orden anulada no debe inflar el monto
            # histórico comprado al proveedor. Separado por moneda porque un
            # proveedor puede tener OC en más de una. String, como en ``results``.
            resumen["monto_por_moneda"] = [
                {"moneda": fila["moneda__codigo_iso"], "total": f"{fila['total']:.2f}"}
                for fila in qs.exclude(estatus=OrdenCompra.EstatusOrdenCompra.CANCELADA)
                .order_by()
                .values("moneda__codigo_iso")
                .annotate(total=Sum("gran_total"))
            ]
        response.data["resumen"] = resumen
        return Response(response.data)

    def _semaforo(self, valor, meta):
        if valor is None:
            return "sin_datos"
        if valor >= meta:
            return "verde"
        if valor >= meta - 10:
            return "amarillo"
        return "rojo"

    @action(detail=False, methods=["get"], url_path="kpis")
    def kpis(self, request):
        """Scorecard por proveedor (EC-436): puntualidad, calidad, cumplimiento
        de cantidad y diferencia de precio, todo contra datos reales de
        compras (``Recepcion``/``CalidadInspeccionDetalle``/
        ``FacturaProveedorDetalle``). ``scorecard`` es el promedio simple de
        los factores que sí tienen dato para ese proveedor -- si falta uno
        (p. ej. nunca le han facturado), se re-normaliza entre los demás en
        vez de inventar un valor."""
        from compras.models import CalidadInspeccionDetalle, OrdenCompra, OrdenCompraDetalle, Recepcion, RecepcionDetalle
        from finanzas.models import FacturaProveedor, FacturaProveedorDetalle

        user = request.user
        empresa = getattr(user, "empresa", None)
        if empresa is None and not getattr(user, "is_superuser", False):
            return Response({"generado_en": timezone.now(), "proveedores": []})

        por_proveedor = {}

        def _bucket(pid, nombre):
            return por_proveedor.setdefault(pid, {
                "proveedor_id": pid, "proveedor_nombre": nombre,
                "recepciones_total": 0, "recepciones_con_compromiso": 0, "recepciones_a_tiempo": 0,
                "dias_reales": [], "dias_pactados": [],
                "cantidad_rechazada": Decimal("0"), "cantidad_inspeccionada": Decimal("0"),
                "cantidad_ordenada": Decimal("0"), "cantidad_recibida": Decimal("0"),
                "facturado": Decimal("0"), "pactado_oc": Decimal("0"),
            })

        # 1) Puntualidad + lead time: una fila por recepción contra su OC.
        recepciones = (
            Recepcion.objects.filter(
                empresa=empresa, activo=True, tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA,
                proveedor__isnull=False, orden_compra__isnull=False,
            )
            .exclude(estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .values(
                "proveedor_id", "proveedor__nombre", "fecha_recepcion",
                "orden_compra__fecha_oc", "orden_compra__fecha_entrega_estimada",
            )
        )
        for row in recepciones:
            b = _bucket(row["proveedor_id"], row["proveedor__nombre"])
            b["recepciones_total"] += 1
            fecha_recepcion = row["fecha_recepcion"].date() if row["fecha_recepcion"] else None
            fecha_compromiso = row["orden_compra__fecha_entrega_estimada"]
            fecha_oc = row["orden_compra__fecha_oc"]
            # Sin fecha prometida no se puede saber si llegó tarde: no cuenta (#387).
            if fecha_compromiso:
                b["recepciones_con_compromiso"] += 1
                if fecha_recepcion and fecha_recepcion <= fecha_compromiso:
                    b["recepciones_a_tiempo"] += 1
            if fecha_recepcion and fecha_oc:
                b["dias_reales"].append((fecha_recepcion - fecha_oc).days)
            if fecha_compromiso and fecha_oc:
                b["dias_pactados"].append((fecha_compromiso - fecha_oc).days)

        # 2) Calidad: rechazado vs. inspeccionado.
        for row in (
            CalidadInspeccionDetalle.objects.filter(
                recepcion_detalle__recepcion__empresa=empresa, recepcion_detalle__recepcion__activo=True,
                recepcion_detalle__recepcion__proveedor__isnull=False,
            )
            .exclude(recepcion_detalle__recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .values("recepcion_detalle__recepcion__proveedor_id", "recepcion_detalle__recepcion__proveedor__nombre")
            .annotate(rechazada=Sum("cantidad_rechazada"), inspeccionada=Sum("cantidad_inspeccionada"))
        ):
            b = _bucket(row["recepcion_detalle__recepcion__proveedor_id"], row["recepcion_detalle__recepcion__proveedor__nombre"])
            b["cantidad_rechazada"] += row["rechazada"] or 0
            b["cantidad_inspeccionada"] += row["inspeccionada"] or 0

        # 3) Cumplimiento de cantidad: ordenado vs. recibido, OCs comprometidas.
        estatus_comprometidas = [
            OrdenCompra.EstatusOrdenCompra.AUTORIZADA,
            OrdenCompra.EstatusOrdenCompra.PARCIALMENTE_RECIBIDA,
            OrdenCompra.EstatusOrdenCompra.RECIBIDA,
        ]
        for row in (
            OrdenCompraDetalle.objects.filter(
                orden_compra__empresa=empresa, orden_compra__activo=True,
                orden_compra__estatus__in=estatus_comprometidas, orden_compra__proveedor__isnull=False,
            )
            .values("orden_compra__proveedor_id", "orden_compra__proveedor__nombre")
            .annotate(ordenado=Sum("cantidad"))
        ):
            b = _bucket(row["orden_compra__proveedor_id"], row["orden_compra__proveedor__nombre"])
            b["cantidad_ordenada"] += row["ordenado"] or 0

        for row in (
            RecepcionDetalle.objects.filter(
                orden_compra_detalle__orden_compra__empresa=empresa,
                orden_compra_detalle__orden_compra__activo=True,
                orden_compra_detalle__orden_compra__estatus__in=estatus_comprometidas,
                orden_compra_detalle__orden_compra__proveedor__isnull=False,
                recepcion__activo=True, recepcion__tipo_origen=Recepcion.TipoOrigen.ORDEN_COMPRA,
            )
            .exclude(recepcion__estatus=Recepcion.EstatusRecepcion.CANCELADA)
            .values(
                "orden_compra_detalle__orden_compra__proveedor_id",
                "orden_compra_detalle__orden_compra__proveedor__nombre",
            )
            .annotate(recibido=Sum("cantidad_recibida"))
        ):
            b = _bucket(
                row["orden_compra_detalle__orden_compra__proveedor_id"],
                row["orden_compra_detalle__orden_compra__proveedor__nombre"],
            )
            b["cantidad_recibida"] += row["recibido"] or 0

        # 4) Diferencia de precio: facturado vs. pactado en la OC.
        money = DecimalField(max_digits=18, decimal_places=2)
        for row in (
            FacturaProveedorDetalle.objects.filter(
                factura_proveedor__empresa=empresa, factura_proveedor__proveedor__isnull=False,
            )
            .exclude(factura_proveedor__estatus=FacturaProveedor.FacturaProveedorStatus.CANCELADA)
            .annotate(
                facturado_linea=ExpressionWrapper(F("precio_unitario") * F("cantidad"), output_field=money),
                pactado_linea=ExpressionWrapper(F("oc_detalle__precio") * F("cantidad"), output_field=money),
            )
            .values("factura_proveedor__proveedor_id", "factura_proveedor__proveedor__nombre")
            .annotate(facturado=Sum("facturado_linea"), pactado=Sum("pactado_linea"))
        ):
            b = _bucket(row["factura_proveedor__proveedor_id"], row["factura_proveedor__proveedor__nombre"])
            b["facturado"] += row["facturado"] or Decimal("0")
            b["pactado_oc"] += row["pactado"] or Decimal("0")

        proveedores = []
        for b in por_proveedor.values():
            pct_a_tiempo = (
                round(b["recepciones_a_tiempo"] / b["recepciones_con_compromiso"] * 100, 1)
                if b["recepciones_con_compromiso"] else None
            )
            dias_real_prom = round(sum(b["dias_reales"]) / len(b["dias_reales"]), 1) if b["dias_reales"] else None
            dias_pactado_prom = (
                round(sum(b["dias_pactados"]) / len(b["dias_pactados"]), 1) if b["dias_pactados"] else None
            )
            pct_rechazo = (
                round(float(b["cantidad_rechazada"]) / float(b["cantidad_inspeccionada"]) * 100, 1)
                if b["cantidad_inspeccionada"] else None
            )
            pct_cumplimiento = (
                round(float(b["cantidad_recibida"]) / float(b["cantidad_ordenada"]) * 100, 1)
                if b["cantidad_ordenada"] else None
            )
            pct_diferencia_precio = (
                round(float(b["facturado"] - b["pactado_oc"]) / float(b["pactado_oc"]) * 100, 1)
                if b["pactado_oc"] else None
            )

            factores = []
            if pct_a_tiempo is not None:
                factores.append(pct_a_tiempo)
            if pct_rechazo is not None:
                factores.append(max(0.0, 100 - pct_rechazo))
            if pct_cumplimiento is not None:
                factores.append(min(pct_cumplimiento, 100.0))
            if pct_diferencia_precio is not None:
                factores.append(max(0.0, 100 - abs(pct_diferencia_precio)))
            scorecard = round(sum(factores) / len(factores), 1) if factores else None

            proveedores.append({
                "proveedor_id": b["proveedor_id"],
                "proveedor_nombre": b["proveedor_nombre"],
                "entrega_a_tiempo": {
                    "pct": pct_a_tiempo,
                    "recepciones_a_tiempo": b["recepciones_a_tiempo"],
                    "recepciones_total": b["recepciones_total"],
                    "recepciones_con_compromiso": b["recepciones_con_compromiso"],
                },
                "calidad": {
                    "pct_rechazado": pct_rechazo,
                    "cantidad_rechazada": b["cantidad_rechazada"],
                    "cantidad_inspeccionada": b["cantidad_inspeccionada"],
                },
                "lead_time": {
                    "dias_promedio_real": dias_real_prom,
                    "dias_promedio_pactado": dias_pactado_prom,
                },
                "cumplimiento_cantidad": {
                    "pct": pct_cumplimiento,
                    "cantidad_ordenada": b["cantidad_ordenada"],
                    "cantidad_recibida": b["cantidad_recibida"],
                },
                "diferencia_precio": {"pct": pct_diferencia_precio},
                "scorecard": {"puntaje": scorecard, "semaforo": self._semaforo(scorecard, 80)},
            })

        proveedores.sort(key=lambda r: (r["scorecard"]["puntaje"] is None, -(r["scorecard"]["puntaje"] or 0)))
        return Response({"generado_en": timezone.now(), "proveedores": proveedores[:50]})

class DireccionClienteViewSet(viewsets.ModelViewSet):
    queryset = DireccionCliente.objects.filter(activo=True)
    serializer_class = DireccionClienteSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if getattr(user, "is_superuser", False):
            return qs
        empresa = getattr(user, "empresa", None)
        if empresa:
            return qs.filter(empresa=empresa)
        return qs.none()

    def perform_destroy(self, instance):
        instance.soft_delete()



