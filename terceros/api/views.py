from rest_framework import viewsets
from rest_framework.response import Response
from django.conf import settings
from django.db.models import Count, Sum
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
        pedidos_qs = Pedido.objects.filter(cliente=cliente, activo=True)

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
            "total_cotizaciones": Cotizacion.objects.filter(cliente=cliente).count(),
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

    def perform_destroy(self, instance):
        instance.soft_delete()

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



