import math
from datetime import datetime, time, timedelta
from decimal import Decimal

from rest_framework import viewsets
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from terceros.models import Proveedor, Cliente, DireccionCliente
from terceros.api.serializers import (
    ClienteMesaControlSerializer,
    ClienteSerializer,
    DireccionClienteSerializer,
    ProveedorSerializer,
    verificar_rfc_cliente,
)
from nucleo.kpis import TOPE_DRILL_DOWN, entero_param, rango_fechas, validar_parametros
from nucleo.listados import (
    OrdenamientoEstableFilter,
    OrdenamientoNullsAlFinal,
    PaginacionOpcionalLimitOffset,
    booleano_param,
)
from terceros.cartera import cartera_de_cliente
from terceros import csf, facturama
from finanzas.services.facturama.acceso import puede_consultar_cfdis
from finanzas.services.facturama.client import FacturamaClient
from finanzas.services.facturama.exceptions import FacturamaAPIException
from terceros.scope import clientes_base, clientes_con_cartera_visible, clientes_visibles, ve_cartera_de_todos
import logging
from nucleo.permisos import CODIGOS_CLIENTES, CODIGOS_PEDIDO_NEUTRO, LecturaPorCodigo

logger = logging.getLogger(__name__)


class ClienteViewSetMesaControl(viewsets.ModelViewSet):
    queryset = clientes_base()
    serializer_class = ClienteMesaControlSerializer
    http_method_names = ['get']

    def get_queryset(self):
        # Mismo alcance que ``ClienteViewSet`` y que el buscador global: una sola
        # definición en ``terceros.scope``. Antes reconstruía desde
        # ``Cliente.objects``, con lo que perdía el ``activo=True`` de su propio
        # ``queryset`` de clase y servía clientes borrados; tampoco aplicaba el
        # scope por ``vendedores`` ni la política de superusuario.
        # ``vendedores`` en una sola consulta, igual que ``ClienteViewSet``: sin
        # el prefetch era una consulta por cliente.
        # ``fecha_ultima_compra``: ``ventas.scope.anotar_fecha_ultima_compra``, la
        # única definición de la subquery (EC-853). Import local por el ciclo
        # ``ventas`` -> ``terceros``.
        from ventas.scope import anotar_fecha_ultima_compra

        return anotar_fecha_ultima_compra(
            clientes_visibles(super().get_queryset(), self.request.user).prefetch_related("vendedores")
        )

class ClienteViewSet(viewsets.ModelViewSet):
    # #1020: lecturas con código R-*; el detalle también desde ``/orders/[id]``.
    permission_classes = [IsAuthenticated, LecturaPorCodigo]
    codigos_lectura = {
        "list": CODIGOS_CLIENTES,
        "kpis": CODIGOS_CLIENTES,
        "retrieve": CODIGOS_CLIENTES + CODIGOS_PEDIDO_NEUTRO,
    }
    queryset = clientes_base()
    serializer_class = ClienteSerializer
    # Modo servidor como ``ProveedorViewSet`` (EC-757): sin ``?limit=`` regresa la
    # lista completa; ``?search=`` busca por nombre, razón social, RFC, clave o correo.
    pagination_class = PaginacionOpcionalLimitOffset
    filter_backends = [
        *[b for b in viewsets.ModelViewSet.filter_backends if b is not OrderingFilter],
        OrdenamientoNullsAlFinal,
    ]
    search_fields = ["nombre", "razon_social", "rfc", "clave", "correo"]
    # Sólo campos de ``Cliente``: ``fecha_alta``, ``fecha_ultima_compra`` y
    # ``updated_at`` venían de la lista de proveedores y no existen aquí; al estar
    # en la lista blanca llegaban al ``order_by`` y respondían 500 (EC-826).
    # NULL al final en ambas direcciones (``created_at`` y ``clave`` son NULL-able).
    ordering_fields = [
        "id", "clave", "nombre", "razon_social", "rfc", "correo", "telefono", "ciudad", "estado",
        "giro_empresarial", "dias_credito", "created_at",
    ]

    def get_queryset(self):
        # El predicado de aislamiento vive en ``terceros.scope`` para que el
        # buscador global (``/api/v1/search/``) reutilice EXACTAMENTE éste y no
        # una copia que pueda separarse de él. ``vendedores`` (M2M del serializer)
        # en una sola consulta, no una por cliente. Orden por defecto: nombre, con
        # ``id`` como desempate estable (antes era indefinido, EC-757).
        return (
            clientes_visibles(super().get_queryset(), self.request.user)
            .prefetch_related("vendedores")
            .order_by("nombre", "id")
        )

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
        from ventas.services.pedido_field_filter_service import puede_ver_contabilidad

        # Misma regla de importes que el listado y el detalle de pedidos (EC-784):
        # sin ella no se agregan ``montos_por_moneda`` ni el ``gran_total`` de cada
        # pedido (ni se corre su agregación); conteos, folios y fechas quedan.
        # Las claves de departamento ya las consultó el alcance (``clientes_visibles``)
        # y quedaron guardadas en el usuario: no se consultan dos veces.
        ver_importes = puede_ver_contabilidad(self.request.user)
        ver_cartera = ve_cartera_de_todos(self.request.user) or cliente.vendedores.filter(
            id=self.request.user.id
        ).exists()

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
        recientes = pedidos_qs.select_related("moneda").order_by("-created_at")[:5].values(
            "id", "folio", "created_at", "estatus", "gran_total", "moneda__codigo_iso"
        )
        pedidos_recientes = []
        for fila in recientes:
            pedido = {
                "id": fila["id"],
                "folio": fila["folio"],
                "fecha": fila["created_at"],
                "estatus": fila["estatus"],
                "estatus_display": estatus_pedido.get(fila["estatus"], fila["estatus"]),
                "moneda": fila["moneda__codigo_iso"],
            }
            if ver_importes:
                pedido["gran_total"] = fila["gran_total"]
            pedidos_recientes.append(pedido)

        resumen = {
            "total_pedidos": pedidos_qs.count(),
            "total_cotizaciones": Cotizacion.objects.filter(
                cliente=cliente, empresa_id=cliente.empresa_id
            ).count(),
            "pedidos_por_estatus": pedidos_por_estatus,
            "ultimo_pedido": pedidos_recientes[0] if pedidos_recientes else None,
            "pedidos_recientes": pedidos_recientes,
            # EC-789: Contabilidad ve la de todos; el resto, solo de sus clientes.
            "cartera": cartera_de_cliente(cliente) if ver_cartera else None,
            # Señal explícita para el front, como los ``kpis/`` (#841).
            "puede_ver_importes": ver_importes,
            "puede_ver_cartera": ver_cartera,
        }
        if ver_importes:
            # Excluye CANCELADO (5) para no inflar el monto histórico con pedidos
            # que nunca se concretaron; se separa por moneda porque un cliente
            # puede tener pedidos en más de una.
            resumen["montos_por_moneda"] = [
                {"moneda": fila["moneda__codigo_iso"], "total": fila["total"]}
                for fila in pedidos_qs.exclude(estatus=5)
                .values("moneda__codigo_iso")
                .annotate(total=Sum("gran_total"))
            ]
        return resumen

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
        serializer.facturama_sincronizado = facturama.sincronizar(cliente)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        data = dict(serializer.data)
        # ``True`` si quedó en Facturama; ``False`` si la empresa no usa Facturama.
        data["facturama_sincronizado"] = getattr(serializer, "facturama_sincronizado", None)
        return Response(data, status=201, headers=self.get_success_headers(serializer.data))

    @transaction.atomic
    def perform_update(self, serializer):
        facturama.sincronizar(serializer.save())

    @transaction.atomic
    def perform_destroy(self, instance):
        facturama.eliminar(instance)
        instance.soft_delete()

    @action(detail=True, methods=["get"], url_path="facturama")
    def en_facturama(self, request, pk=None):
        """El cliente tal como está en Facturama (``GET /Client/{Id}``)."""
        return Response(facturama.consultar(self.get_object()))

    @action(detail=False, methods=["post"], url_path="importar-csf", parser_classes=[MultiPartParser])
    def importar_csf(self, request):
        """Lee una Constancia de Situación Fiscal (PDF, campo ``archivo``) y devuelve los datos
        para prellenar el alta. No crea ni guarda nada: el alta sigue siendo ``POST /clientes/``."""
        archivo = request.FILES.get("archivo")
        if archivo is None:
            raise ValidationError({"archivo": "Adjunta la constancia en PDF."})
        if archivo.size > csf.MAX_BYTES:
            raise ValidationError({"archivo": "El PDF excede 5 MB."})
        if archivo.read(5) != b"%PDF-":
            raise ValidationError({"archivo": "El archivo debe ser un PDF."})
        archivo.seek(0)
        try:
            datos = csf.leer_csf(archivo)
        except csf.CsfInvalida as e:
            raise ValidationError({"archivo": str(e)})
        existente = clientes_visibles(clientes_base(), request.user).filter(
            rfc=datos["rfc"], activo=True
        ).values_list("id", flat=True).first()
        return Response({"datos": datos, "cliente_existente_id": existente})

    # ``detail=False`` fuera de ``get_queryset()`` a propósito (EC-422): mismo
    # criterio que los demás ``kpis`` -- todo con ``aggregate()``/``Sum``/
    # ``Count`` en DB, nunca iterando clientes/facturas/CxC fila por fila.
    @action(detail=False, methods=["get"], url_path="kpis")
    def kpis(self, request):
        user = request.user
        validar_parametros(request, set())  # sin filtros: un parámetro → 400
        empresa = getattr(user, "empresa", None)
        if empresa is None:
            return Response(self._kpis_vacio())

        # Mismo alcance que el listado (``clientes_visibles``): un vendedor
        # normal solo ve los KPIs de SUS clientes, Mesa de Control/admin ven
        # toda la empresa. Import local: ver nota en ``_resumen_comercial``.
        clientes_qs = clientes_visibles(clientes_base(), user)
        # Facturación y cartera (EC-789): Contabilidad, de todos; el resto, de sus clientes.
        ve_todos = ve_cartera_de_todos(user)
        con_importes = clientes_con_cartera_visible(clientes_qs, user, ve_todos)
        data = {
            "generado_en": timezone.now(),
            "puede_ver_cartera_de_todos": ve_todos,
            "ventas_por_cliente": self._kpi_ventas_por_cliente(con_importes),
            "clientes_activos": self._kpi_clientes_activos(clientes_qs),
            # Mismo alcance que ``clientes_activos`` (EC-817), no el de importes.
            "clientes_nuevos": self._kpi_clientes_nuevos(clientes_qs),
            "reclamos_devoluciones": {
                "disponible": False,
                "motivo": "Devolucion/DevolucionDetalle no registran cantidad de piezas (solo los FKs); Entrega tampoco, así que no hay piezas devueltas ni piezas embarcadas que dividir.",
            },
            "cartera_antiguedad": self._kpi_cartera_antiguedad(con_importes),
        }
        return Response(data)

    @action(detail=False, methods=["get"], url_path="verificar-rfc")
    def verificar_rfc(self, request):
        """Botón "Verificar RFC" (EC-570, CL-04): ``?rfc=`` da el mismo resultado y
        el mismo mensaje que guardar ese RFC en el campo ``rfc``. Sin ``cliente``,
        como un alta en la empresa del usuario; con ``?cliente=<id>``, como la
        edición de ese cliente (regla de su empresa; 404 si es de otra empresa).
        No revisa duplicados ni consulta Facturama."""
        validar_parametros(request, {"rfc", "cliente"})
        rfc = (request.query_params.get("rfc") or "").strip().upper()
        if not rfc:
            raise ValidationError({"rfc": "Indica el RFC a verificar."})
        empresa = getattr(request.user, "empresa", None)
        cliente_id = entero_param(request, "cliente")
        if cliente_id is not None:
            clientes = clientes_base()
            clientes = clientes.filter(empresa=empresa) if empresa else clientes.none()
            empresa = get_object_or_404(clientes.select_related("empresa"), pk=cliente_id).empresa
        # Primero la validación del campo (longitud máxima), como en el guardado.
        try:
            self.get_serializer().fields["rfc"].run_validation(rfc)
        except ValidationError as exc:
            aplica, error = False, str(exc.detail[0])
        else:
            aplica, error = verificar_rfc_cliente(rfc, empresa)
        return Response({
            "rfc": rfc,
            "valido": error is None,
            "error": error,
            "formato_verificado": aplica,
        })

    def _kpis_vacio(self):
        motivo = {"disponible": False, "motivo": "Usuario sin empresa asignada."}
        return {
            "generado_en": timezone.now(),
            "ventas_por_cliente": motivo,
            "clientes_activos": motivo,
            "clientes_nuevos": motivo,
            "reclamos_devoluciones": motivo,
            "cartera_antiguedad": motivo,
        }

    def _kpi_ventas_por_cliente(self, clientes_qs):
        from finanzas.models import Factura

        # Facturación = facturas Emitidas (ni Borrador ni Cancelada) y activas
        # (#367). Por moneda (#370): una factura en USD no se suma con las MXN.
        filas = list(
            Factura.objects.filter(
                cliente__in=clientes_qs, empresa=F("cliente__empresa"), activo=True,
                estatus=Factura.FacturaStatus.EMITIDA,
            )
            .values("moneda__codigo_iso", "cliente_id", "cliente__nombre")
            .annotate(monto=Sum("total"))
            .order_by("moneda__codigo_iso", "-monto", "cliente_id")
        )
        por_moneda = {}
        for fila in filas:
            por_moneda.setdefault(fila["moneda__codigo_iso"], []).append(fila)

        monedas = []
        for moneda, clientes in por_moneda.items():
            gran_total = sum((f["monto"] or Decimal("0")) for f in clientes)
            top_clientes = []
            acumulado = Decimal("0")
            for fila in clientes[:5]:
                monto = fila["monto"] or Decimal("0")
                acumulado += monto
                top_clientes.append({
                    "cliente_id": fila["cliente_id"],
                    "cliente_nombre": fila["cliente__nombre"],
                    "monto": monto,
                    "pct_del_total": round(float(monto) / float(gran_total) * 100, 1) if gran_total else 0.0,
                    "pct_acumulado": round(float(acumulado) / float(gran_total) * 100, 1) if gran_total else 0.0,
                })
            monedas.append({
                "moneda": moneda,
                "total_facturado": gran_total,
                "total_clientes_facturados": len(clientes),
                "top_clientes": top_clientes,
            })

        return {
            "disponible": True,
            "criterio": "Facturas Emitidas y activas.",
            "por_moneda": monedas,
        }

    # Actividad comercial = venta real (EC-835): la misma regla que
    # ``fecha_ultima_compra`` de Mesa de Control (``ventas.scope.pedidos_venta_real``).
    VENTANA_ACTIVIDAD_DIAS = 90

    def _kpi_clientes_activos(self, clientes_qs):
        from django.db.models import Exists, OuterRef

        from ventas.models import Pedido
        from ventas.scope import pedidos_venta_real

        desde = timezone.now() - timedelta(days=self.VENTANA_ACTIVIDAD_DIAS)
        # Una sola definición de "activo" para los conteos Y las listas (#371).
        pedidos = pedidos_venta_real(Pedido.objects.filter(
            cliente=OuterRef("pk"), empresa=OuterRef("empresa"), activo=True, created_at__gte=desde,
        ))
        activos_qs = clientes_qs.filter(Exists(pedidos))
        inactivos_qs = clientes_qs.exclude(Exists(pedidos))
        activos = activos_qs.count()
        inactivos = inactivos_qs.count()
        total = activos + inactivos

        def _filas(qs, orden):
            return [
                {"cliente_id": c["id"], "cliente_nombre": c["nombre"]}
                for c in qs.order_by(*orden).values("id", "nombre")[:TOPE_DRILL_DOWN]
            ]

        drill_activos = _filas(activos_qs, ("nombre", "id"))
        drill_inactivos = _filas(inactivos_qs, ("nombre", "id"))
        return {
            "disponible": True,
            "criterio": "Activo = al menos una venta real en la ventana: pedido Autorizado o En proceso, sin muestras ni pedidos de error.",
            "total": total,
            "activos": activos,
            "inactivos": inactivos,
            "pct_activos": round(activos / total * 100, 1) if total else None,
            "ventana_dias": self.VENTANA_ACTIVIDAD_DIAS,
            "drill_down": {
                "activos": drill_activos,
                "inactivos": drill_inactivos,
                "activos_truncado": activos > len(drill_activos),
                "inactivos_truncado": inactivos > len(drill_inactivos),
            },
        }

    def _kpi_clientes_nuevos(self, clientes_qs):
        # Mes calendario en la hora local del proyecto (America/Mexico_City), como
        # los demás "del mes" (``timezone.localdate().replace(day=1)``): con
        # límites en UTC, un alta del día 1 antes de las 06:00Z caería en el mes
        # anterior. Los clientes sin ``created_at`` (anteriores al historial) no
        # cuentan en ningún mes.
        # ``hasta`` se devuelve inclusivo (último día del mes), como ``fecha_hasta``
        # en el resto del API; la consulta usa ``< 1° del mes siguiente``.
        desde = timezone.localdate().replace(day=1)
        siguiente = (desde + timedelta(days=32)).replace(day=1)
        total = clientes_qs.filter(
            created_at__gte=timezone.make_aware(datetime.combine(desde, time.min)),
            created_at__lt=timezone.make_aware(datetime.combine(siguiente, time.min)),
        ).count()
        return {
            "disponible": True,
            "criterio": "Clientes activos dados de alta en el mes en curso (hora del centro de México).",
            "total": total,
            "mes": desde.strftime("%Y-%m"),
            "desde": desde,
            "hasta": siguiente - timedelta(days=1),
        }

    def _kpi_cartera_antiguedad(self, clientes_qs):
        from finanzas.models import CuentaPorCobrar, Factura

        hoy = timezone.localdate()
        hace_30 = hoy - timedelta(days=30)
        hace_60 = hoy - timedelta(days=60)
        # Solo documentos de la empresa del cliente: uno de otra empresa que lo
        # apunte no entra (mismo criterio que ``_resumen_comercial``). Sin
        # saldos de facturas canceladas o dadas de baja (#368), y por moneda de
        # la factura (#370).
        vivas = CuentaPorCobrar.objects.filter(
            cliente__in=clientes_qs,
            empresa=F("cliente__empresa"),
            estatus__in=[CuentaPorCobrar.EstatusCxC.PENDIENTE, CuentaPorCobrar.EstatusCxC.PARCIAL],
            factura__activo=True,
        ).exclude(factura__estatus=Factura.FacturaStatus.CANCELADA)
        vencidas = vivas.filter(fecha_vencimiento__isnull=False, fecha_vencimiento__lt=hoy)
        moneda = "factura__moneda__codigo_iso"

        por_moneda = [
            {
                "moneda": fila[moneda],
                "saldo_total_vencido": fila["saldo_total"] or Decimal("0"),
                "total_cuentas_vencidas": fila["total_cuentas"] or 0,
                "buckets": {
                    "0_30": {"total": fila["b_0_30_total"] or 0, "monto": fila["b_0_30_monto"] or Decimal("0")},
                    "31_60": {"total": fila["b_31_60_total"] or 0, "monto": fila["b_31_60_monto"] or Decimal("0")},
                    "60_mas": {"total": fila["b_60_mas_total"] or 0, "monto": fila["b_60_mas_monto"] or Decimal("0")},
                },
            }
            for fila in vencidas.values(moneda).annotate(
                total_cuentas=Count("id"),
                saldo_total=Sum("saldo"),
                b_0_30_total=Count("id", filter=Q(fecha_vencimiento__gte=hace_30)),
                b_0_30_monto=Sum("saldo", filter=Q(fecha_vencimiento__gte=hace_30)),
                b_31_60_total=Count("id", filter=Q(fecha_vencimiento__lt=hace_30, fecha_vencimiento__gte=hace_60)),
                b_31_60_monto=Sum("saldo", filter=Q(fecha_vencimiento__lt=hace_30, fecha_vencimiento__gte=hace_60)),
                b_60_mas_total=Count("id", filter=Q(fecha_vencimiento__lt=hace_60)),
                b_60_mas_monto=Sum("saldo", filter=Q(fecha_vencimiento__lt=hace_60)),
            ).order_by(moneda)
        ]
        # Saldo vivo sin fecha de vencimiento: no se puede decir si está
        # vencido, así que va aparte en vez de perderse en silencio (#368).
        sin_vencimiento = [
            {"moneda": fila[moneda], "total": fila["total"], "monto": fila["monto"] or Decimal("0")}
            for fila in vivas.filter(fecha_vencimiento__isnull=True).values(moneda)
            .annotate(total=Count("id"), monto=Sum("saldo")).order_by(moneda)
        ]
        total_vencidas = vencidas.count()
        drill_down = list(
            vencidas.select_related("cliente")
            .order_by("fecha_vencimiento", "id")
            .values("id", "cliente_id", "cliente__nombre", "saldo", "fecha_vencimiento", moneda)[:TOPE_DRILL_DOWN]
        )
        for fila in drill_down:
            fila["moneda"] = fila.pop(moneda)
            fila["dias_vencida"] = (hoy - fila["fecha_vencimiento"]).days

        return {
            "disponible": True,
            "total_cuentas_vencidas": total_vencidas,
            "por_moneda": por_moneda,
            "sin_vencimiento": sin_vencimiento,
            "drill_down": drill_down,
            "drill_down_total": total_vencidas,
            "drill_down_truncado": total_vencidas > len(drill_down),
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
    queryset = Proveedor.objects.all()
    serializer_class = ProveedorSerializer
    # ``?search=`` se publicaba en el schema y se ignoraba sin esto (#839).
    search_fields = ["codigo", "nombre", "razon_social", "rfc"]
    pagination_class = PaginacionOpcionalLimitOffset  # sin ?limit= regresa la lista completa
    filter_backends = [
        *[b for b in viewsets.ModelViewSet.filter_backends if b is not OrderingFilter],
        OrdenamientoEstableFilter,
    ]
    # Lista blanca de ``?ordering=`` (#863).
    ordering_fields = [
        "id", "codigo", "nombre", "razon_social", "rfc", "tipo", "ciudad", "estado", "contacto_principal",
        "email", "telefono", "fecha_alta", "fecha_baja", "fecha_ultima_compra", "plazo_dias", "dias_credito",
        "activo", "created_at", "updated_at",
    ]
    # Las bajas que lista ``?activo=false`` también se abren: detalle, historial
    # y ``kpis-detalle`` en solo lectura; editarlas o darlas de baja es 400 (#888).
    ACCIONES_CON_BAJAS = {
        "retrieve", "historial_ordenes_compra", "kpis_detalle", "update", "partial_update", "destroy",
    }

    def get_queryset(self):
        user = self.request.user
        # Listado más reciente primero por fecha de alta del proveedor;
        # ``-id`` como desempate estable. Siempre la empresa del usuario,
        # superusuario incluido: mismo alcance que el listado de OC (#834).
        qs = super().get_queryset().order_by("-fecha_alta", "-id")
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa)
        if self.action == "list":
            # ``?activo=false`` lista las bajas (#863).
            qs = qs.filter(activo=booleano_param(self.request, "activo", True))
            if "es_maquilero" in self.request.query_params:
                qs = qs.filter(es_maquilero=booleano_param(self.request, "es_maquilero", True))
            return qs
        if self.action in self.ACCIONES_CON_BAJAS:
            return qs
        return qs.filter(activo=True)

    @staticmethod
    def _exigir_activo(proveedor):
        if not proveedor.activo:
            raise ValidationError({"detail": "El proveedor está dado de baja."})

    def perform_create(self, serializer):
        empresa = getattr(self.request.user, "empresa", None)
        if empresa is None:
            raise ValidationError({"empresa": "El usuario no tiene una empresa asignada."})
        with transaction.atomic():
            serializer.save(empresa=empresa, codigo=Proveedor.siguiente_codigo(empresa))

    def perform_update(self, serializer):
        self._exigir_activo(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._exigir_activo(instance)
        instance.activo = False
        instance.fecha_baja = timezone.localdate()
        instance.save(update_fields=["activo", "fecha_baja"])

    @action(detail=True, methods=["get"], url_path="historial-ordenes-compra")
    def historial_ordenes_compra(self, request, pk=None):
        """OC de este proveedor para su detalle (EC-399). El proveedor se acota
        con ``get_object``; las OC, por la empresa del usuario con el mismo
        criterio que el listado de OC (superusuario incluido)."""
        from compras.models import OrdenCompra
        from compras.api.serializers import OrdenCompraSerializer, con_pre_ordenes
        from compras.services.orden_compra_view_service import (
            puede_ver_contabilidad,
            quitar_montos_orden_compra,
        )

        proveedor = self.get_object()
        empresa = getattr(request.user, "empresa", None)
        qs = (
            OrdenCompra.objects.filter(proveedor=proveedor, empresa=empresa, activo=True)
            .select_related("empresa", "sucursal", "moneda", "usuario", "pedido", "proveedor")
            .prefetch_related("pedidos")
        ) if empresa is not None else OrdenCompra.objects.none()
        qs = con_pre_ordenes(qs)

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
        # Permiso evaluado una vez; las filas se limpian en sitio (#836).
        ver_montos = puede_ver_contabilidad(request.user)
        data = OrdenCompraSerializer(page, many=True).data
        if not ver_montos:
            for item in data:
                quitar_montos_orden_compra(item)
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

    PARAMS_KPIS = {"proveedor", "sucursal", "fecha_desde", "fecha_hasta", "meta_scorecard", "limite"}

    def _alcance_kpis(self, request):
        """Mismo alcance que el listado de proveedores: la empresa del usuario,
        superusuario incluido (#834); ``None`` si no hay empresa."""
        from compras.services.kpi_criterios import AlcanceCompras

        empresa = getattr(request.user, "empresa", None)
        if empresa is None:
            return None
        fecha_desde, fecha_hasta = rango_fechas(request)
        return AlcanceCompras(
            empresa=empresa,
            sucursal_id=entero_param(request, "sucursal"),
            proveedor_id=entero_param(request, "proveedor"),
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
            solo_proveedores_activos=True,
        )

    @action(detail=False, methods=["get"], url_path="kpis")
    def kpis(self, request):
        """Scorecard por proveedor (EC-436): puntualidad, calidad, cumplimiento
        de cantidad y diferencia de precio, todo contra datos reales de
        compras. ``scorecard`` es el promedio simple de los factores que sí
        tienen dato para ese proveedor -- si falta uno (p. ej. nunca le han
        facturado), se re-normaliza entre los demás en vez de inventar un
        valor. Criterios en ``compras.services.kpi_proveedores``/``kpi_criterios``
        (compartidos con ``recepciones/kpis/``)."""
        from compras.services.kpi_proveedores import (
            LIMITE_PROVEEDORES,
            META_SCORECARD,
            TOPE_LIMITE_PROVEEDORES,
            calcular_scorecards,
        )

        validar_parametros(request, self.PARAMS_KPIS)
        meta = self._numero_param(request, "meta_scorecard", META_SCORECARD, 0, 100)
        # Con signo (EC-829): ``-5`` llega al rango y responde su mensaje, como ``0``.
        limite = entero_param(request, "limite", con_signo=True)
        limite = LIMITE_PROVEEDORES if limite is None else limite
        if not 1 <= limite <= TOPE_LIMITE_PROVEEDORES:
            raise ValidationError({"limite": f"Debe estar entre 1 y {TOPE_LIMITE_PROVEEDORES}."})

        alcance = self._alcance_kpis(request)
        if alcance is None:
            # Distinto de "sin datos": el usuario no tiene empresa.
            return Response({
                "generado_en": timezone.now(),
                "disponible": False,
                "motivo": "Usuario sin empresa asignada.",
                # Clave fija del contrato; sin datos no hay importes (#860).
                "puede_ver_importes": False,
                "filtros": None,
                "proveedores": [],
                "total_proveedores": 0,
                "truncado": False,
            })

        from compras.services.orden_compra_view_service import puede_ver_contabilidad
        from nucleo.importes import quitar_montos

        puede_ver_importes = puede_ver_contabilidad(request.user)
        proveedores = calcular_scorecards(alcance, meta)
        if not puede_ver_importes:
            # ``pct`` se queda: es porcentaje, no importe (#859).
            for proveedor in proveedores:
                quitar_montos(proveedor["diferencia_precio"], ["costo_facturado", "costo_pactado_oc"])
        return Response({
            "generado_en": timezone.now(),
            "disponible": True,
            # Señal explícita para el front (#841), mismo criterio que OC.
            "puede_ver_importes": puede_ver_importes,
            "filtros": {**alcance.como_dict(), "meta_scorecard": meta, "limite": limite},
            "proveedores": proveedores[:limite],
            "total_proveedores": len(proveedores),
            "truncado": len(proveedores) > limite,
        })

    @action(detail=True, methods=["get"], url_path="kpis-detalle")
    def kpis_detalle(self, request, pk=None):
        """Registros detrás de un factor del scorecard de este proveedor:
        ``?factor=puntualidad|calidad|cumplimiento|precio`` (+ los mismos
        filtros de ``kpis``). Acotado a ``TOPE_DETALLE`` con ``total``."""
        from compras.services.kpi_criterios import AlcanceCompras
        from compras.services.kpi_proveedores import FACTORES_DETALLE, TOPE_DETALLE, detalle_proveedor
        from compras.services.orden_compra_view_service import puede_ver_contabilidad
        from nucleo.importes import quitar_montos

        validar_parametros(request, {"factor", "sucursal", "fecha_desde", "fecha_hasta"})
        factor = (request.query_params.get("factor") or "").strip()
        if factor not in FACTORES_DETALLE:
            raise ValidationError({"factor": f"Debe ser uno de: {', '.join(FACTORES_DETALLE)}."})
        proveedor = self.get_object()  # 404 si es de otra empresa
        fecha_desde, fecha_hasta = rango_fechas(request)
        alcance = AlcanceCompras(
            empresa=proveedor.empresa,
            sucursal_id=entero_param(request, "sucursal"),
            proveedor_id=proveedor.pk,
            fecha_desde=fecha_desde,
            fecha_hasta=fecha_hasta,
        )
        filas, total = detalle_proveedor(alcance, factor)
        data = {
            "proveedor_id": proveedor.pk,
            "factor": factor,
            "puede_ver_importes": puede_ver_contabilidad(request.user),
            "total": total,
            "truncado": total > len(filas),
            "tope": TOPE_DETALLE,
            "registros": filas,
        }
        if not data["puede_ver_importes"]:
            # Precios unitarios de ``factor=precio``, como en ``kpis/`` (#859).
            quitar_montos(data, ["precio_facturado", "precio_pactado"])
        return Response(data)

    @staticmethod
    def _numero_param(request, nombre, default, minimo, maximo):
        valor = request.query_params.get(nombre)
        if valor in (None, ""):
            return default
        try:
            numero = float(valor)
        except ValueError:
            raise ValidationError({nombre: "Debe ser un número."})
        if not math.isfinite(numero) or not minimo <= numero <= maximo:
            raise ValidationError({nombre: f"Debe ser un número entre {minimo} y {maximo}."})
        return numero


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                "cliente", OpenApiTypes.INT,
                description="Solo las direcciones de ese cliente (pk). De otra empresa o inexistente: "
                "lista vacía. No entero: 400. Sin él, todas las de la empresa del usuario "
                "(el superusuario, las de todas).",
            ),
        ],
        responses={
            200: DireccionClienteSerializer(many=True),
            400: OpenApiResponse(
                description='``cliente`` no entero: {"cliente": "Debe ser un entero válido."}',
            ),
        },
    ),
)
class DireccionClienteViewSet(viewsets.ModelViewSet):
    queryset = DireccionCliente.objects.filter(activo=True)
    serializer_class = DireccionClienteSerializer

    def get_queryset(self):
        # Nivel empresa, no por vendedor: quien edita una cotización necesita las
        # direcciones de su cliente aunque no lo tenga asignado. Se exige también
        # la empresa del cliente porque ``empresa`` de la dirección venía del
        # payload antes de EC-570.
        user = self.request.user
        # Orden por defecto estable: la predeterminada arriba y ``id`` de desempate
        # (antes indefinido, EC-775). Un ``?ordering=`` válido lo reemplaza: el
        # ``OrderingFilter`` global hace su propio ``order_by``.
        qs = super().get_queryset().order_by("-is_default", "id")
        # ``?cliente=`` del listado (EC-775). Se valida antes del alcance para que
        # un valor no entero sea 400 para todos, también sin empresa.
        cliente_id = entero_param(self.request, "cliente") if self.action == "list" else None
        empresa = getattr(user, "empresa", None)
        if not empresa:
            return qs.none()
        qs = qs.filter(empresa=empresa, cliente__empresa=empresa)
        # El filtro va después del alcance: un cliente de otra empresa no trae
        # filas, igual que un id inexistente.
        if cliente_id is not None:
            qs = qs.filter(cliente_id=cliente_id)
        return qs

    def perform_create(self, serializer):
        # ``validate_cliente`` ya garantizó que el cliente es de la empresa y tiene empresa.
        serializer.save(empresa=serializer.validated_data["cliente"].empresa)

    def perform_update(self, serializer):
        # Un cliente heredado sin empresa conserva la de la dirección: nunca NULL.
        cliente = serializer.validated_data.get("cliente", serializer.instance.cliente)
        serializer.save(empresa=cliente.empresa if cliente.empresa_id else serializer.instance.empresa)

    def perform_destroy(self, instance):
        instance.soft_delete()





class PuedeVerificarRfc(BasePermission):
    message = "No tienes acceso a la verificación de RFC en Facturama."

    def has_permission(self, request, view):
        return puede_consultar_cfdis(request.user)


class VerificarRfcView(APIView):
    """GET ``?rfc=`` -> estatus del RFC en el SAT vía Facturama (EC-534).

    Misma regla de acceso que la consulta de CFDIs: empresa dueña de la cuenta
    de Facturama + ``R-CONTABILIDAD-FACTURACION`` (superusuario siempre)."""

    permission_classes = [IsAuthenticated, PuedeVerificarRfc]

    def get(self, request):
        rfc = (request.query_params.get("rfc") or "").strip().upper()
        if not rfc:
            raise ValidationError({"rfc": "Este parámetro es obligatorio."})
        try:
            data = FacturamaClient().get("customers/status", params={"rfc": rfc}) or {}
        except FacturamaAPIException as exc:
            code = exc.status_code if exc.status_code and exc.status_code < 500 else 502
            return Response({"error": exc.code, "message": exc.message}, status=code)
        return Response({
            "Rfc": data.get("Rfc") or rfc,
            "FormatoCorrecto": bool(data.get("FormatoCorrecto")),
            "Activo": bool(data.get("Activo")),
            "Localizado": bool(data.get("Localizado")),
        })
