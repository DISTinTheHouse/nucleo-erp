from decimal import Decimal

from django.db import OperationalError, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.fields import get_error_detail
from rest_framework.response import Response

from finanzas.exceptions import (
    CONCURRENCY_SQLSTATES,
    CONCURRENT_OPERATION_MESSAGE,
    ConcurrentOperationError,
    ErrorDeNegocio,
    database_error_sqlstate,
)
from finanzas.models import (
    AlertaMora,
    Banco,
    CentroCosto,
    Cobro,
    CobroDetalle,
    ConciliacionBancaria,
    CuentaBancaria,
    CuentaContable,
    CuentaPorCobrar,
    CuentaPorPagar,
    Factura,
    FacturaDetalle,
    FacturaProveedor,
    FacturaProveedorDetalle,
    MovimientoBancario,
    NotaCredito,
    NotaCreditoDetalle,
    Pago,
    PagoDetalle,
    Poliza,
    PolizaDetalle,
)

from finanzas.api.serializers import (
    AlertaMoraSerializer,
    BancoSerializer,
    CentroCostoSerializer,
    CobroSerializer,
    ConciliacionBancariaSerializer,
    ConciliacionPrepararInputSerializer,
    CuentaBancariaSerializer,
    CuentaContableSerializer,
    CuentaPorCobrarDetalleSerializer,
    CuentaPorCobrarSerializer,
    CuentaPorPagarCreateSerializer,
    CuentaPorPagarSerializer,
    FacturaSerializer,
    FacturaDesdePedidoInputSerializer,
    FacturaPendienteCobroInputSerializer,
    FacturaProveedorSerializer,
    MovimientoBancarioSerializer,
    NotaCreditoSerializer,
    PagoSerializer,
    PolizaSerializer,
)

from finanzas.services.alerta_mora_service import AlertaMoraService
from finanzas.services.cobro_service import CobroService
from finanzas.services.conciliacion_service import ConciliacionService
from finanzas.services.cuenta_por_pagar_service import CuentaPorPagarService
from finanzas.services.dashboard_service import DashboardFinancieroService
from finanzas.services.factura_service import FacturaService
from finanzas.services.movimiento_bancario_service import MovimientoBancarioService
from finanzas.services.nota_credito_service import NotaCreditoService
from finanzas.services.pago_service import PagoService
from finanzas.services.poliza_service import PolizaService
from finanzas.utils.folios import generate_factura_folio
from nucleo.models import Moneda, Sucursal
from ventas.models import Pedido, PedidoDetalle
from terceros.models import Cliente
from terceros.api.serializers import ClienteSerializer


def _bool_param(v):
    return (str(v or "").strip().lower() in {"1", "true", "si", "sí", "yes"})


def _puede_ver_todo(user):
    """Quién ve fuera de su empresa: SÓLO el superusuario.

    ``is_admin_empresa`` NO entra aquí. En el patrón canónico del proyecto ese
    flag relaja un sub-alcance MÁS ESTRECHO dentro de la empresa —el vendedor en
    ``ventas.scope.alcance_cotizaciones()``, la sucursal en
    ``wms``/``produccion.get_queryset()``— y nunca quita el filtro por empresa.
    Incluirlo en este predicado abría los 17 endpoints de finanzas a los
    registros de las demás empresas del tenant compartido, en contra de lo que
    ya documentaban ``_aplicar_scope_empresa`` y ``_resolve_empresa`` aquí abajo.
    """
    return bool(getattr(user, "is_superuser", False))


def _user_empresa_or_none(user):
    """Devuelve user.empresa. Para superuser devuelve None (no hay scoping).
    Para usuarios no-superuser sin empresa asignada lanza PermissionDenied."""
    if _puede_ver_todo(user):
        return None
    empresa = getattr(user, "empresa", None)
    if empresa is None:
        raise PermissionDenied("Usuario sin empresa asignada.")
    return empresa


def _aplicar_scope_empresa(qs, user, lookup="empresa"):
    """Aplica filtro multi-tenant al queryset.
    - is_superuser → bypass  (devuelve qs sin tocar scope empresa)
    - is_admin_empresa / normal → filtra por user.empresa mediante lookup (default 'empresa').
      Si no tiene empresa → devuelve queryset .none()
    """
    if _puede_ver_todo(user):
        return qs
    empresa = getattr(user, "empresa", None)
    if empresa is None:
        return qs.none()
    return qs.filter(**{lookup: empresa})


def _resolve_empresa(user, validated_data=None, *, required=True, allow_user_empresa_fallback=True):
    """Resuelve la empresa para operaciones write (create/update/action).

    Reglas:
    - is_superuser:
        * Si validated_data trae "empresa" → la usa (puede crear para cualquier empresa).
        * Si NO la trae y required=True → ValidationError pidiendo empresa explícita.
        * Si NO la trae y required=False → None.
    - is_admin_empresa / usuario normal:
        * Requiere user.empresa (si no → PermissionDenied).
        * Si validated_data trae "empresa" DEBE coincidir con user.empresa o ValidationError.
        * Fallback a user.empresa si validated_data no trae empresa.
    """
    validated_data = validated_data or {}
    superuser = _puede_ver_todo(user)
    data_emp = validated_data.get("empresa") if validated_data else None
    user_emp = getattr(user, "empresa", None)

    if superuser:
        if data_emp is None:
            if required:
                raise ValidationError(
                    {"empresa": "Superusuario debe especificar la empresa explícitamente."}
                )
            return None
        return data_emp

    if user_emp is None:
        raise PermissionDenied("Usuario sin empresa asignada.")

    if data_emp is not None and getattr(data_emp, "pk", data_emp) != user_emp.pk:
        raise ValidationError({"empresa": "La empresa indicada no coincide con su usuario."})

    if allow_user_empresa_fallback:
        return data_emp or user_emp
    return data_emp if data_emp is not None else user_emp


def _validate_fk_empresa(user, related_obj, field_name, user_emp):
    """Valida que un FK pertenezca a user_emp. Para superuser no valida."""
    if _puede_ver_todo(user):
        return
    if related_obj is None or user_emp is None:
        return
    obj_emp = getattr(related_obj, "empresa_id", None)
    if obj_emp is None:
        return
    if obj_emp != user_emp.pk:
        raise ValidationError(
            {field_name: f"{field_name.replace('_', ' ').capitalize()} no pertenece a la empresa."}
        )


def _attr_chain(obj, *attrs):
    current = obj
    for attr in attrs:
        current = getattr(current, attr, None)
        if current is None:
            return None
    return current


def _empresa_id_relacionada(obj):
    """Empresa de un objeto, directa o vía FK padre (varios detalles no la cargan directo)."""
    for chain in (
        ("empresa_id",),
        ("factura", "empresa_id"),
        ("factura_proveedor", "empresa_id"),
        ("cuenta_bancaria", "empresa_id"),
        ("orden_compra", "empresa_id"),
        ("recepcion", "empresa_id"),
        ("poliza", "empresa_id"),
    ):
        empresa_id = _attr_chain(obj, *chain)
        if empresa_id is not None:
            return empresa_id
    return None


def _validate_related_parent_empresa(related_obj, field_name, parent_empresa):
    """Línea hija debe pertenecer a la empresa del documento padre. Corre siempre, incluso superuser."""
    if related_obj is None or parent_empresa is None:
        return
    related_empresa_id = _empresa_id_relacionada(related_obj)
    parent_empresa_id = getattr(parent_empresa, "pk", parent_empresa)
    if related_empresa_id is not None and related_empresa_id != parent_empresa_id:
        raise ValidationError(
            {field_name: [f"{field_name.replace('_', ' ').capitalize()} no pertenece a la misma empresa del documento."]}
        )


class ErroresDeNegocioComo400Mixin:
    """Traduce el ``ErrorDeNegocio`` que lanzan los servicios a la 400 de DRF.

    Los servicios de ``finanzas`` no pueden levantar la ``ValidationError`` de
    DRF —deben seguir siendo agnósticos del framework—, así que DRF no las
    conocía y las dejaba escapar como un 500. Cada regla de negocio violada
    (importe que excede el saldo, pago sin detalle, póliza descuadrada) llegaba
    al frontend como un error opaco. Antes del alta de líneas anidadas esos
    caminos eran inalcanzables; ahora son de uso diario.

    Se convierte **sólo** ``ErrorDeNegocio``, no la ``ValidationError`` de Django
    a secas: un ``Model.clean()``, un validador de campo o una librería de
    terceros levantan esa misma clase ante datos corruptos o un bug de
    configuración, y eso es un 500 legítimo que debe verse en monitoreo, no un
    400 que el cliente no puede accionar. La subclase es la que marca "esto sí es
    para el cliente".

    Se traduce aquí, en la frontera HTTP de finanzas, y no con un
    ``EXCEPTION_HANDLER`` global, para no cambiar el comportamiento de las demás
    apps. Va en ``handle_exception`` y no en cada ``perform_*`` porque cubre de
    una sola vez las ~26 llamadas a servicios repartidas entre ``perform_create``,
    ``perform_update``, ``perform_destroy`` y las acciones ``@action``, sin que
    una ruta nueva se quede fuera por olvido.

    Se conserva la forma del mensaje: los servicios lanzan diccionarios como
    ``{"cobro_detalles": [...]}`` y así llegan al cliente, que puede mapear el
    error a su campo. La conversión la hace ``get_error_detail`` de DRF, que
    además preserva el ``code`` de cada error e interpola sus ``params``.
    """

    def handle_exception(self, exc):
        if isinstance(exc, ErrorDeNegocio):
            exc = ValidationError(get_error_detail(exc))
        return super().handle_exception(exc)


class ConcurrentOperationConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = CONCURRENT_OPERATION_MESSAGE
    default_code = "operacion_concurrente"


class ConcurrencyConflictsAs409Mixin:
    """Traduce los choques con otra transacción a 409 en lugar de un 500.

    Dos fuentes, y sólo ésas:

    - ``ConcurrentOperationError``: un servicio pidió sus bloqueos con NOWAIT y
      otra transacción ya los tenía (el guard de borrado de CxP).
    - Un ``OperationalError`` de Postgres con SQLSTATE 40P01 (deadlock) o 55P03
      (bloqueo no disponible), venga de donde venga. ``PagoService`` bloquea en dos
      órdenes opuestos, así que un deadlock residual sigue siendo posible: Postgres
      aborta a una de las transacciones y aquí sale como algo que el cliente puede
      reintentar.

    Cualquier otro ``DatabaseError`` sigue saliendo como 500. El 409 es el de
    ``hr`` y ``produccion`` (subclase de ``APIException``); el cuerpo es una lista
    con el mensaje, como los demás errores sin campo de finanzas.
    """

    def handle_exception(self, exc):
        if isinstance(exc, ConcurrentOperationError):
            exc = ConcurrentOperationConflict([str(exc)])
        elif (
            isinstance(exc, OperationalError)
            and database_error_sqlstate(exc) in CONCURRENCY_SQLSTATES
        ):
            exc = ConcurrentOperationConflict([CONCURRENT_OPERATION_MESSAGE])
        return super().handle_exception(exc)


# Los tres viewsets base de finanzas. Existen para que la traducción de errores
# de negocio sea por construcción y no por memoria: antes el mixin se aplicaba
# clase por clase y siete viewsets se habían quedado fuera, de modo que volvían a
# responder 500 en cuanto su ``perform_*`` llamara a un servicio. Cada base
# conserva la clase de DRF que ya usaba el viewset, así que ninguna superficie
# HTTP cambia: ``AlertaMoraViewSet`` sigue siendo de sólo lectura y
# ``DashboardFinancieroViewSet`` sigue sin rutas de detalle. El mapeo de choques
# de concurrencia a 409 va en las mismas bases por la misma razón.
class FinanzasBaseViewSet(ConcurrencyConflictsAs409Mixin, ErroresDeNegocioComo400Mixin, viewsets.ModelViewSet):
    pass


class FinanzasBaseReadOnlyViewSet(
    ConcurrencyConflictsAs409Mixin, ErroresDeNegocioComo400Mixin, viewsets.ReadOnlyModelViewSet
):
    pass


class FinanzasBaseSimpleViewSet(ConcurrencyConflictsAs409Mixin, ErroresDeNegocioComo400Mixin, viewsets.ViewSet):
    pass


def _aplicar_filtros_fecha(qs, params, fecha_campo="fecha"):
    fi = params.get("fecha_inicio") or params.get("fecha_desde")
    ff = params.get("fecha_fin") or params.get("fecha_hasta")
    if fi:
        qs = qs.filter(**{f"{fecha_campo}__gte": fi})
    if ff:
        qs = qs.filter(**{f"{fecha_campo}__lte": ff})
    return qs


def _aplicar_ordering(qs, params, default):
    ordering = (params.get("ordering") or "").strip()
    if not ordering:
        return qs.order_by(*default)
    campos_validos = {f.lstrip("-") for f in default}
    campo = ordering.lstrip("-")
    if campo in campos_validos:
        return qs.order_by(ordering)
    return qs.order_by(*default)


class ClienteViewSetContabilidad(FinanzasBaseViewSet):
    queryset = Cliente.objects.filter(activo=True)
    serializer_class = ClienteSerializer
    http_method_names = ['get']

    def get_queryset(self):
        user = self.request.user
        qs = Cliente.objects.all()
        qs = _aplicar_scope_empresa(qs, user, lookup="empresa")
        qp = self.request.query_params
        nombre = (qp.get("nombre") or "").strip()
        if nombre:
            qs = qs.filter(Q(nombre__icontains=nombre) | Q(razon_social__icontains=nombre))
        activo_only = _bool_param(qp.get("activo")) or True
        if activo_only:
            qs = qs.filter(activo=True)
        return qs


ClienteViewSet = ClienteViewSetContabilidad


class CuentaPorCobrarViewSet(FinanzasBaseViewSet):
    serializer_class = CuentaPorCobrarSerializer
    http_method_names = ['get', 'post', 'put', 'patch']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return CuentaPorCobrarDetalleSerializer
        return CuentaPorCobrarSerializer

    def get_queryset(self):
        user = self.request.user
        superuser = _puede_ver_todo(user)
        empresa = getattr(user, 'empresa', None)

        qs = (
            CuentaPorCobrar.objects.select_related(
                'cliente',
                'factura',
                'factura__moneda',
                'factura__cliente',
                'factura__sucursal',
            )
        )
        if not superuser:
            if empresa is None:
                return CuentaPorCobrar.objects.none()
            qs = qs.filter(
                Q(empresa=empresa) | Q(empresa__isnull=True, factura__empresa=empresa)
            ).distinct()

        qp = self.request.query_params
        cliente_id = qp.get('cliente') or qp.get('cliente_id')
        estatus = (qp.get('estatus') or '').strip()
        saldo_pendiente = qp.get('saldo_pendiente')
        vencidas = qp.get('vencidas')
        folio = (qp.get('folio') or "").strip()
        moneda = qp.get('moneda') or qp.get('moneda_id')
        factura_id = qp.get('factura') or qp.get('factura_id')

        if cliente_id:
            qs = qs.filter(cliente_id=cliente_id)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if _bool_param(saldo_pendiente):
            qs = qs.filter(saldo__gt=0)
        if _bool_param(vencidas):
            qs = qs.filter(
                fecha_vencimiento__isnull=False,
                fecha_vencimiento__lt=timezone.localdate(),
                saldo__gt=0,
            ).exclude(estatus=CuentaPorCobrar.EstatusCxC.CANCELADA)
        if folio:
            qs = qs.filter(factura__folio__icontains=folio)
        if moneda:
            qs = qs.filter(factura__moneda_id=moneda)
        if factura_id:
            qs = qs.filter(factura_id=factura_id)
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha_emision")
        qs = _aplicar_ordering(qs, qp, ["-fecha_emision", "-id"])
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        superuser = _puede_ver_todo(user)
        empresa = getattr(user, "empresa", None)
        if not superuser and empresa is None:
            raise PermissionDenied("Usuario sin empresa.")
        factura = serializer.validated_data.get("factura")
        if not superuser:
            if factura and getattr(factura, "empresa_id", None) and factura.empresa_id != empresa.pk:
                raise ValidationError({"factura": "Factura no pertenece a la empresa."})
            cliente = serializer.validated_data.get("cliente")
            if cliente and getattr(cliente, "empresa_id", None) and cliente.empresa_id != empresa.pk:
                raise ValidationError({"cliente": "Cliente no pertenece a la empresa."})
        emp = serializer.validated_data.get("empresa") or (empresa if not superuser else serializer.validated_data.get("empresa"))
        if not superuser:
            serializer.save(empresa=emp or empresa)
        else:
            if emp is None:
                raise ValidationError({"empresa": "Superusuario debe especificar empresa."})
            serializer.save(empresa=emp)


class FacturaViewSet(FinanzasBaseViewSet):
    serializer_class = FacturaSerializer
    http_method_names = ['delete', 'get', 'post', 'put', 'patch']

    def get_queryset(self):
        user = self.request.user
        qs = (
            Factura.objects
            .select_related('pedido', 'cliente', 'moneda')
        )
        qs = _aplicar_scope_empresa(qs, user, lookup="empresa")
        qp = self.request.query_params
        cliente_id = qp.get('cliente') or qp.get('cliente_id')
        estatus = (qp.get('estatus') or '').strip()
        folio = (qp.get('folio') or '').strip()
        moneda = qp.get('moneda') or qp.get('moneda_id')
        pedido = qp.get('pedido') or qp.get('pedido_id')
        saldo_pendiente = qp.get('saldo_pendiente')
        if cliente_id:
            qs = qs.filter(cliente_id=cliente_id)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if folio:
            qs = qs.filter(folio__icontains=folio)
        if moneda:
            qs = qs.filter(moneda_id=moneda)
        if pedido:
            qs = qs.filter(pedido_id=pedido)
        if _bool_param(saldo_pendiente):
            qs = qs.filter(cuentas_por_cobrar__saldo__gt=0).distinct()
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha_emision")
        qs = _aplicar_ordering(qs, qp, ["-fecha_emision", "-id"])
        return qs

    def perform_destroy(self, instance):
        instance.soft_delete()

    def _get_default_sucursal(self, user, empresa):
        sucursal = getattr(user, "sucursal_default", None)
        if sucursal and getattr(sucursal, "empresa_id", None) == getattr(empresa, "pk", None):
            return sucursal
        return Sucursal.objects.filter(empresa=empresa, activo=True).order_by("codigo").first()

    def _get_default_centro_costo(self, empresa):
        return (
            CentroCosto.objects.filter(empresa=empresa, activo=True)
            .order_by("codigo", "id")
            .first()
        )

    def _get_poliza_folio(self, empresa, sucursal):
        ultima = (
            Poliza.objects.filter(empresa=empresa, sucursal=sucursal)
            .order_by("-folio_consecutivo", "-id")
            .first()
        )
        consecutivo = (getattr(ultima, "folio_consecutivo", 0) or 0) + 1
        return f"POL-{consecutivo:06d}", consecutivo

    def _get_poliza_cuentas(self, empresa, impuestos):
        cuentas = CuentaContable.objects.filter(
            empresa=empresa,
            activo=True,
            acepta_movimientos=True,
        )
        cuenta_cxc = cuentas.filter(tipo=CuentaContable.CuentaTipo.ACTIVO).order_by("codigo", "id").first()
        cuenta_ingreso = cuentas.filter(tipo=CuentaContable.CuentaTipo.INGRESO).order_by("codigo", "id").first()
        cuenta_impuesto = None
        if impuestos > Decimal("0"):
            cuenta_impuesto = cuentas.filter(tipo=CuentaContable.CuentaTipo.PASIVO).order_by("codigo", "id").first()

        errores = {}
        if cuenta_cxc is None:
            errores["cuenta_contable_cxc"] = "No existe una cuenta contable activa de tipo Activo para registrar cuentas por cobrar."
        if cuenta_ingreso is None:
            errores["cuenta_contable_ingreso"] = "No existe una cuenta contable activa de tipo Ingreso para registrar la factura."
        if impuestos > Decimal("0") and cuenta_impuesto is None:
            errores["cuenta_contable_impuesto"] = "No existe una cuenta contable activa de tipo Pasivo para registrar impuestos."
        if errores:
            raise ValidationError(errores)

        return cuenta_cxc, cuenta_ingreso, cuenta_impuesto

    def _crear_poliza_factura_pendiente(self, *, empresa, sucursal, user, factura, cxc):
        centro_costo = self._get_default_centro_costo(empresa)
        if centro_costo is None:
            raise ValidationError(
                {"centro_costo": "No existe un centro de costo activo para generar la póliza contable."}
            )

        cuenta_cxc, cuenta_ingreso, cuenta_impuesto = self._get_poliza_cuentas(
            empresa,
            factura.impuestos or Decimal("0"),
        )
        folio_poliza, folio_consecutivo = self._get_poliza_folio(empresa, sucursal)
        referencia = cxc.referencia or factura.folio or str(factura.pk)
        ingreso_neto = (factura.subtotal or Decimal("0")) - (factura.descuento or Decimal("0"))

        poliza = Poliza.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            centro_costo=centro_costo,
            folio=folio_poliza,
            folio_consecutivo=folio_consecutivo,
            tipo=Poliza.PolizaTipo.INGRESO,
            concepto=f"Factura por cobrar {factura.folio or factura.pk} - {factura.cliente.nombre}"[:200],
            usuario_creacion=user,
        )

        detalles = [
            PolizaDetalle(
                poliza=poliza,
                cuenta_contable=cuenta_cxc,
                centro_costo=centro_costo,
                factura=factura,
                cargo=factura.total,
                abono=Decimal("0.00"),
                referencia=referencia,
                observaciones=f"Cargo por cuenta por cobrar de factura {factura.folio or factura.pk}.",
                orden=1,
            )
        ]
        if ingreso_neto > Decimal("0"):
            detalles.append(
                PolizaDetalle(
                    poliza=poliza,
                    cuenta_contable=cuenta_ingreso,
                    centro_costo=centro_costo,
                    factura=factura,
                    cargo=Decimal("0.00"),
                    abono=ingreso_neto,
                    referencia=referencia,
                    observaciones=f"Abono por ingreso de factura {factura.folio or factura.pk}.",
                    orden=2,
                )
            )
        if (factura.impuestos or Decimal("0")) > Decimal("0"):
            detalles.append(
                PolizaDetalle(
                    poliza=poliza,
                    cuenta_contable=cuenta_impuesto,
                    centro_costo=centro_costo,
                    factura=factura,
                    cargo=Decimal("0.00"),
                    abono=factura.impuestos,
                    referencia=referencia,
                    observaciones=f"Abono por impuestos de factura {factura.folio or factura.pk}.",
                    orden=len(detalles) + 1,
                )
            )

        PolizaDetalle.objects.bulk_create(detalles)
        PolizaService.validar_suma_cero(poliza)
        return poliza

    @action(detail=False, methods=['post'], url_path='registrar-pendiente-cobro')
    def registrar_pendiente_cobro(self, request):
        serializer = FacturaPendienteCobroInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user
        superuser = _puede_ver_todo(user)
        empresa = data.get("empresa") or getattr(user, 'empresa', None)
        if empresa is None:
            if superuser:
                raise ValidationError({'empresa': 'Superusuario debe especificar empresa explícitamente.'})
            raise ValidationError({'empresa': 'El usuario no tiene empresa asignada.'})

        sucursal = self._get_default_sucursal(user, empresa)
        if sucursal is None:
            raise ValidationError({'sucursal': 'No hay una sucursal disponible para registrar la factura.'})

        cliente_qs = Cliente.objects.filter(pk=data['cliente'], activo=True)
        if not superuser:
            cliente_qs = cliente_qs.filter(empresa=empresa)
        cliente = cliente_qs.first()
        if cliente is None:
            raise ValidationError({'cliente': 'Cliente no encontrado o sin acceso.'})

        moneda = Moneda.objects.filter(
            pk=data['moneda'],
            activo=True,
        ).filter(
            Q(empresa__isnull=True) | Q(empresa=empresa)
        ).first()
        if moneda is None:
            raise ValidationError({'moneda': 'Moneda no encontrada o sin acceso.'})

        pedido = None
        pedido_id = data.get('pedido')
        if pedido_id:
            pedido_qs = Pedido.objects.filter(pk=pedido_id)
            if not superuser:
                pedido_qs = pedido_qs.filter(empresa=empresa)
            pedido = pedido_qs.first()
            if pedido is None:
                raise ValidationError({'pedido': 'Pedido no encontrado o sin acceso.'})
            if pedido.cliente_id != cliente.pk:
                raise ValidationError({'pedido': 'El pedido no corresponde al cliente indicado.'})
            if pedido.moneda_id != moneda.pk:
                raise ValidationError({'pedido': 'El pedido no corresponde a la moneda indicada.'})

        folio = (data.get('folio') or '').strip()
        if folio and Factura.objects.filter(empresa=empresa, folio=folio, activo=True).exists():
            raise ValidationError({'folio': 'Ya existe una factura activa con ese folio.'})
        if not folio:
            folio = generate_factura_folio(empresa, sucursal)

        with transaction.atomic():
            if pedido is not None:
                pedido_qs_for_update = Pedido.objects.select_for_update().filter(pk=pedido.pk)
                if not superuser:
                    pedido_qs_for_update = pedido_qs_for_update.filter(empresa=empresa)
                pedido = pedido_qs_for_update.first()
                if pedido is None:
                    raise ValidationError({'pedido': 'Pedido no encontrado o sin acceso.'})
                ya_facturado = (
                    Factura.objects.filter(pedido=pedido, activo=True)
                    .exclude(estatus=Factura.FacturaStatus.CANCELADA)
                    .exists()
                )
                if ya_facturado:
                    raise ValidationError({
                        'pedido': 'El pedido ya tiene una factura activa; no puede facturarse más de una vez.'
                    })

            factura = Factura.objects.create(
                empresa=empresa,
                sucursal=sucursal,
                cliente=cliente,
                pedido=pedido,
                moneda=moneda,
                folio=folio,
                fecha_vencimiento=data.get('fecha_vencimiento'),
                subtotal=data['subtotal'],
                descuento=data['descuento'],
                impuestos=data['impuestos'],
                total=data['total'],
                estatus=Factura.FacturaStatus.EMITIDA,
                observaciones=data.get('observaciones') or None,
            )
            cxc = CuentaPorCobrar.objects.create(
                empresa=empresa,
                cliente=cliente,
                factura=factura,
                fecha_vencimiento=data.get('fecha_vencimiento'),
                total=data['total'],
                saldo=data['total'],
                estatus=CuentaPorCobrar.EstatusCxC.PENDIENTE,
                referencia=(data.get('referencia') or folio or None),
                observaciones=data.get('observaciones') or None,
            )
            poliza = self._crear_poliza_factura_pendiente(
                empresa=empresa,
                sucursal=sucursal,
                user=user,
                factura=factura,
                cxc=cxc,
            )

        return Response(
            {
                'factura': FacturaSerializer(factura).data,
                'cuenta_por_cobrar': {
                    'id': cxc.pk,
                    'estatus': cxc.estatus,
                    'saldo': str(cxc.saldo),
                    'referencia': cxc.referencia,
                    'fecha_vencimiento': cxc.fecha_vencimiento,
                },
                'poliza': {
                    'id': poliza.pk,
                    'folio': poliza.folio,
                    'tipo': poliza.tipo,
                    'estatus': poliza.estatus,
                    'detalles': poliza.poliza_detalles.count(),
                },
            },
            status=status.HTTP_201_CREATED,
        )

    def _acotar_onboarding_a_empresa(self, empresa, validated_data):
        pedido_recibido = validated_data.get('pedido')
        if pedido_recibido is None:
            raise ValidationError({'pedido': 'El pedido es obligatorio.'})

        pedido = (
            Pedido.objects.select_for_update()
            .filter(pk=pedido_recibido.pk, empresa=empresa)
            .first()
        )
        if pedido is None:
            raise NotFound('El pedido no existe o no pertenece a tu empresa.')

        ya_facturado = (
            Factura.objects.filter(pedido=pedido, activo=True)
            .exclude(estatus=Factura.FacturaStatus.CANCELADA)
            .exists()
        )
        if ya_facturado:
            raise ValidationError({
                'pedido': 'El pedido ya tiene una factura activa; no puede facturarse más de una vez.'
            })

        detalles = validated_data.get('factura_detalles') or []
        if not detalles:
            raise ValidationError({
                'factura_detalles': 'La factura debe incluir al menos una línea.'
            })

        ids_detalle = {fila['pedido_detalle'].pk for fila in detalles}
        ids_propios = set(
            PedidoDetalle.objects.filter(
                pk__in=ids_detalle,
                pedido=pedido,
            ).values_list('pk', flat=True)
        )
        ajenos = sorted(ids_detalle - ids_propios)
        if ajenos:
            raise ValidationError({
                'factura_detalles': f'Líneas que no pertenecen al pedido indicado: {ajenos}.'
            })

        serie_folio = validated_data.get('serie_folio')
        if serie_folio is not None and serie_folio.empresa_id != empresa.pk:
            raise ValidationError({
                'serie_folio': 'Serie de folio no encontrada o sin acceso.'
            })

        validated_data['pedido'] = pedido
        return pedido

    @action(detail=False, methods=['get', 'post'], url_path='onboarding', url_name='onboarding')
    def onboarding(self, request):
        if request.method == 'GET':
            queryset = self.filter_queryset(self.get_queryset())
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        elif request.method == 'POST':
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            empresa = getattr(request.user, 'empresa', None)
            if empresa is None:
                raise ValidationError({'empresa': 'El usuario no tiene empresa asignada.'})

            sucursal = self._get_default_sucursal(request.user, empresa)
            if sucursal is None:
                raise ValidationError({'sucursal': 'No hay una sucursal disponible para registrar la factura.'})

            with transaction.atomic():
                self._acotar_onboarding_a_empresa(empresa, serializer.validated_data)
                factura = FacturaService.store_factura(
                    request.user, serializer.validated_data, sucursal
                )

            serializer = self.get_serializer(factura)
            return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='desde-pedido', url_name='desde-pedido')
    def desde_pedido(self, request):
        input_serializer = FacturaDesdePedidoInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        pedido_id = input_serializer.validated_data['pedido']

        user = request.user
        empresa = getattr(user, 'empresa', None)

        if empresa is None:
            raise ValidationError({'empresa': 'El usuario no tiene empresa asignada.'})

        sucursal = self._get_default_sucursal(user, empresa)
        if sucursal is None:
            raise ValidationError({'sucursal': 'No hay una sucursal disponible para registrar la factura.'})

        with transaction.atomic():
            pedido = (
                Pedido.objects.select_for_update()
                .filter(pk=pedido_id, empresa=empresa)
                .first()
            )
            if pedido is None:
                raise NotFound('El pedido no existe o no pertenece a tu empresa.')

            ya_facturado = (
                Factura.objects.filter(pedido=pedido, activo=True)
                .exclude(estatus=Factura.FacturaStatus.CANCELADA)
                .exists()
            )
            if ya_facturado:
                raise ValidationError({
                    'pedido': 'El pedido ya tiene una factura activa; no puede facturarse más de una vez.'
                })

            factura = self._facturar_pedido_completo(pedido, empresa, sucursal)

        return Response(
            FacturaSerializer(factura).data,
            status=status.HTTP_201_CREATED,
        )

    def _facturar_pedido_completo(self, pedido, empresa, sucursal):
        folio_factura = generate_factura_folio(empresa, sucursal)
        factura = Factura.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            cliente=pedido.cliente,
            moneda=pedido.moneda,
            pedido=pedido,
            folio=folio_factura,
        )

        detalles = (
            PedidoDetalle.objects.filter(pedido=pedido)
            .select_related('producto')
            .prefetch_related('tallas')
            .order_by('id')
        )

        bulk_data = []
        factura_subtotal = Decimal('0.00')
        factura_descuento = Decimal('0.00')
        factura_impuestos = Decimal('0.00')
        factura_total = Decimal('0.00')

        for det in detalles:
            cantidad = Decimal(sum(t.cantidad for t in det.tallas.all()))
            precio_unitario = det.precio_unitario or Decimal('0')

            descuento = Decimal('0.00')
            impuesto = Decimal('0.00')

            subtotal = cantidad * precio_unitario
            total = subtotal - descuento + impuesto

            bulk_data.append(
                FacturaDetalle(
                    factura=factura,
                    pedido_detalle=det,
                    producto=det.producto,
                    cantidad=cantidad,
                    precio_unitario=precio_unitario,
                    descuento=descuento,
                    impuesto=impuesto,
                    subtotal=subtotal,
                    total=total,
                )
            )

            factura_subtotal += subtotal
            factura_descuento += descuento
            factura_impuestos += impuesto
            factura_total += total

        FacturaDetalle.objects.bulk_create(bulk_data)

        factura.subtotal = factura_subtotal
        factura.descuento = factura_descuento
        factura.impuestos = factura_impuestos
        factura.total = factura_total
        factura.save(
            update_fields=['subtotal', 'descuento', 'impuestos', 'total']
        )
        return factura


class CuentaContableViewSet(FinanzasBaseViewSet):
    queryset = CuentaContable.objects.all()
    serializer_class = CuentaContableSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        # El alcance decide QUÉ filas, no CÓMO se filtran: con ``return qs`` en
        # esta rama el superusuario se saltaba también los query params y el
        # ordering, y recibía la lista completa sin filtrar ni ordenar. Misma
        # forma que ``PolizaViewSet``/``BancoViewSet.get_queryset()``.
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        codigo = (qp.get("codigo") or "").strip()
        nombre = (qp.get("nombre") or "").strip()
        tipo = (qp.get("tipo") or "").strip()
        nivel = qp.get("nivel")
        acepta_movimientos = qp.get("acepta_movimientos")
        activo = qp.get("activo")
        if codigo:
            qs = qs.filter(codigo__icontains=codigo)
        if nombre:
            qs = qs.filter(nombre__icontains=nombre)
        if tipo:
            qs = qs.filter(tipo=tipo)
        if nivel:
            qs = qs.filter(nivel=nivel)
        if acepta_movimientos is not None:
            qs = qs.filter(acepta_movimientos=_bool_param(acepta_movimientos))
        if activo is not None and _bool_param(activo):
            qs = qs.filter(activo=True)
        elif activo is not None and not _bool_param(activo):
            qs = qs.filter(activo=False)
        return _aplicar_ordering(qs, qp, ["codigo", "id"])

    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        cp = serializer.validated_data.get("cuenta_padre")
        _validate_fk_empresa(user, cp, "cuenta_padre", empresa)
        serializer.save(empresa=empresa)

    def perform_update(self, serializer):
        user = self.request.user
        superuser = _puede_ver_todo(user)
        if not superuser:
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        superuser = _puede_ver_todo(user)
        if not superuser:
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        instance.delete()


class CentroCostoViewSet(FinanzasBaseViewSet):
    queryset = CentroCosto.objects.all()
    serializer_class = CentroCostoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        # Igual que en ``CuentaContableViewSet``: el alcance no debe llevarse por
        # delante los query params ni el ordering del superusuario.
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        codigo = (qp.get("codigo") or "").strip()
        nombre = (qp.get("nombre") or "").strip()
        activo = qp.get("activo")
        if codigo:
            qs = qs.filter(codigo__icontains=codigo)
        if nombre:
            qs = qs.filter(nombre__icontains=nombre)
        if activo is not None:
            qs = qs.filter(activo=_bool_param(activo))
        return _aplicar_ordering(qs, qp, ["codigo", "id"])

    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        serializer.save(empresa=empresa)

    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        instance.delete()


class PolizaViewSet(FinanzasBaseViewSet):
    queryset = Poliza.objects.all()
    serializer_class = PolizaSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if _puede_ver_todo(user):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        tipo = (qp.get("tipo") or "").strip()
        estatus = (qp.get("estatus") or "").strip()
        folio = (qp.get("folio") or "").strip()
        sucursal = qp.get("sucursal") or qp.get("sucursal_id")
        centro_costo = qp.get("centro_costo") or qp.get("centro_costo_id")
        if tipo:
            qs = qs.filter(tipo=tipo)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if folio:
            qs = qs.filter(folio__icontains=folio)
        if sucursal:
            qs = qs.filter(sucursal_id=sucursal)
        if centro_costo:
            qs = qs.filter(centro_costo_id=centro_costo)
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha")
        return _aplicar_ordering(qs, qp, ["-fecha", "-folio_consecutivo", "-id"])

    # El encabezado y sus renglones entran juntos: las líneas se crean DESPUÉS
    # del ``save()``, así que sin la transacción un renglón rechazado dejaba la
    # póliza ya confirmada y huérfana, con el cliente recibiendo un 400. Mismo
    # decorador que Cobro/Pago/MovimientoBancario/Conciliación/NotaCrédito.
    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        suc = serializer.validated_data.get("sucursal")
        _validate_fk_empresa(user, suc, "sucursal", empresa)
        cc = serializer.validated_data.get("centro_costo")
        _validate_fk_empresa(user, cc, "centro_costo", empresa)
        detalles_data = serializer.validated_data.pop("poliza_detalles", [])
        poliza = serializer.save(
            empresa=empresa,
            usuario_creacion=serializer.validated_data.get("usuario_creacion") or user,
        )
        for detalle_data in detalles_data:
            for field_name in (
                "cuenta_contable",
                "centro_costo",
                "factura",
                "factura_proveedor",
                "pago",
                "cobro",
                "movimiento_bancario",
            ):
                _validate_related_parent_empresa(detalle_data.get(field_name), field_name, empresa)
            PolizaDetalle.objects.create(poliza=poliza, **detalle_data)

    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        if instance.estatus == Poliza.PolizaStatus.CONTABILIZADA:
            raise ValidationError("No se puede eliminar una póliza contabilizada. Cancelela primero.")
        instance.delete()

    @action(detail=True, methods=["post"], url_path="contabilizar")
    def contabilizar(self, request, pk=None):
        poliza = self.get_object()
        PolizaService.contabilizar(poliza)
        return Response(PolizaSerializer(poliza).data)

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        poliza = self.get_object()
        PolizaService.cancelar(poliza)
        return Response(PolizaSerializer(poliza).data)

    @action(detail=True, methods=["post"], url_path="validar-cuadre")
    def validar_cuadre(self, request, pk=None):
        poliza = self.get_object()
        cargos, abonos = PolizaService.validar_suma_cero(poliza)
        return Response({
            "total_cargos": str(cargos),
            "total_abonos": str(abonos),
            "cuadre_correcto": True,
        })


class FacturaProveedorViewSet(FinanzasBaseViewSet):
    queryset = FacturaProveedor.objects.all()
    serializer_class = FacturaProveedorSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if _puede_ver_todo(user):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        proveedor = qp.get("proveedor") or qp.get("proveedor_id")
        estatus = (qp.get("estatus") or "").strip()
        folio = (qp.get("folio") or "").strip()
        moneda = qp.get("moneda") or qp.get("moneda_id")
        oc = qp.get("oc") or qp.get("orden_compra") or qp.get("orden_compra_id")
        recepcion = qp.get("recepcion") or qp.get("recepcion_id")
        if proveedor:
            qs = qs.filter(proveedor_id=proveedor)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if folio:
            qs = qs.filter(folio__icontains=folio)
        if moneda:
            qs = qs.filter(moneda_id=moneda)
        if oc:
            qs = qs.filter(oc_id=oc)
        if recepcion:
            qs = qs.filter(recepcion_id=recepcion)
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha_emision")
        return _aplicar_ordering(qs, qp, ["-fecha_emision", "-id"])

    # Mismo motivo que en ``PolizaViewSet.perform_create``: los renglones se
    # crean tras el ``save()`` del encabezado, y un renglón rechazado dejaba la
    # factura de proveedor huérfana.
    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        oc = serializer.validated_data.get("oc")
        _validate_fk_empresa(user, oc, "oc", empresa)
        recep = serializer.validated_data.get("recepcion")
        _validate_fk_empresa(user, recep, "recepcion", empresa)
        detalles_data = serializer.validated_data.pop("factura_proveedor_detalles", [])
        factura_proveedor = serializer.save(empresa=empresa)
        for detalle_data in detalles_data:
            oc_detalle = detalle_data.get("oc_detalle")
            recepcion_detalle = detalle_data.get("recepcion_detalle")
            producto = detalle_data.get("producto")
            _validate_related_parent_empresa(oc_detalle, "oc_detalle", empresa)
            _validate_related_parent_empresa(recepcion_detalle, "recepcion_detalle", empresa)
            _validate_related_parent_empresa(producto, "producto", empresa)
            if oc_detalle is not None and oc_detalle.orden_compra_id != factura_proveedor.oc_id:
                raise ValidationError({"oc_detalle": ["Detalle de OC no corresponde a la orden de compra de la factura."]})
            if recepcion_detalle is not None and recepcion_detalle.recepcion_id != factura_proveedor.recepcion_id:
                raise ValidationError({"recepcion_detalle": ["Detalle de recepción no corresponde a la recepción de la factura."]})
            if producto is not None and oc_detalle is not None and oc_detalle.producto_id != producto.pk:
                raise ValidationError({"producto": ["Producto no coincide con el detalle de orden de compra."]})
            if producto is not None and recepcion_detalle is not None and recepcion_detalle.producto_id != producto.pk:
                raise ValidationError({"producto": ["Producto no coincide con el detalle de recepción."]})
            FacturaProveedorDetalle.objects.create(
                factura_proveedor=factura_proveedor,
                **detalle_data,
            )
        # Crear la factura directamente como Registrada también la registra.
        if factura_proveedor.estatus == FacturaProveedor.FacturaProveedorStatus.REGISTRADA:
            CuentaPorPagarService.generate_for_invoice(factura_proveedor)

    @transaction.atomic
    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        # La decisión "esta petición es la que registra la factura" se toma con la
        # fila bloqueada, igual que en NotaCreditoViewSet.perform_update: con el
        # estatus del objeto cargado antes del lock, dos registros concurrentes
        # veían ambos "Borrador" e intentaban generar la CxP dos veces.
        locked = FacturaProveedor.objects.select_for_update().filter(pk=serializer.instance.pk).first()
        if locked is None:
            raise NotFound("La factura de proveedor ya no existe.")
        serializer.instance = locked
        previous_status = locked.estatus
        factura_proveedor = serializer.save()
        if (
            factura_proveedor.estatus == FacturaProveedor.FacturaProveedorStatus.REGISTRADA
            and previous_status != FacturaProveedor.FacturaProveedorStatus.REGISTRADA
        ):
            CuentaPorPagarService.generate_for_invoice(factura_proveedor)

    @transaction.atomic
    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        # Borrar la factura arrastra en cascada su CxP y los PagoDetalle de ésta.
        # Se bloquea primero la factura para no competir con un registro
        # concurrente que esté generando su CxP.
        locked = FacturaProveedor.objects.select_for_update().filter(pk=instance.pk).first()
        if locked is None:
            raise NotFound("La factura de proveedor ya no existe.")
        CuentaPorPagarService.ensure_invoice_deletable(locked)
        locked.delete()


class BancoViewSet(FinanzasBaseViewSet):
    queryset = Banco.objects.all()
    serializer_class = BancoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if _puede_ver_todo(user):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        nombre = (qp.get("nombre") or "").strip()
        codigo = (qp.get("codigo") or "").strip()
        swift = (qp.get("swift") or "").strip()
        activo = qp.get("activo")
        if nombre:
            qs = qs.filter(nombre__icontains=nombre)
        if codigo:
            qs = qs.filter(codigo__icontains=codigo)
        if swift:
            qs = qs.filter(swift__icontains=swift)
        if activo is not None:
            qs = qs.filter(activo=_bool_param(activo))
        return _aplicar_ordering(qs, qp, ["nombre", "codigo", "id"])

    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        serializer.save(empresa=empresa)

    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        instance.delete()


class CuentaBancariaViewSet(FinanzasBaseViewSet):
    queryset = CuentaBancaria.objects.all()
    serializer_class = CuentaBancariaSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if _puede_ver_todo(user):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        banco = qp.get("banco") or qp.get("banco_id")
        moneda = qp.get("moneda") or qp.get("moneda_id")
        alias = (qp.get("alias") or "").strip()
        numero = (qp.get("numero_cuenta") or qp.get("numero") or "").strip()
        activo = qp.get("activo")
        if banco:
            qs = qs.filter(banco_id=banco)
        if moneda:
            qs = qs.filter(moneda_id=moneda)
        if alias:
            qs = qs.filter(alias__icontains=alias)
        if numero:
            qs = qs.filter(numero_cuenta__icontains=numero)
        if activo is not None:
            qs = qs.filter(activo=_bool_param(activo))
        return _aplicar_ordering(qs.select_related("banco", "moneda"), qp, ["banco__nombre", "alias", "id"])

    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        banco = serializer.validated_data.get("banco")
        _validate_fk_empresa(user, banco, "banco", empresa)
        moneda = serializer.validated_data.get("moneda")
        _validate_fk_empresa(user, moneda, "moneda", empresa)
        serializer.save(empresa=empresa)

    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        instance.delete()

    @action(detail=True, methods=["get"], url_path="resumen")
    def resumen(self, request, pk=None):
        cuenta = self.get_object()
        return Response(MovimientoBancarioService.resumen_cuenta(cuenta))


class CuentaPorPagarViewSet(FinanzasBaseViewSet):
    queryset = CuentaPorPagar.objects.all()
    serializer_class = CuentaPorPagarSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if _puede_ver_todo(user):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        proveedor = qp.get("proveedor") or qp.get("proveedor_id")
        estatus = (qp.get("estatus") or "").strip()
        saldo_pendiente = qp.get("saldo_pendiente")
        vencidas = qp.get("vencidas")
        folio = (qp.get("folio") or "").strip()
        moneda = qp.get("moneda") or qp.get("moneda_id")
        factura_proveedor = qp.get("factura_proveedor") or qp.get("factura_proveedor_id")
        if proveedor:
            qs = qs.filter(proveedor_id=proveedor)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if _bool_param(saldo_pendiente):
            qs = qs.filter(saldo__gt=0)
        if _bool_param(vencidas):
            qs = qs.filter(
                fecha_vencimiento__isnull=False,
                fecha_vencimiento__lt=timezone.localdate(),
                saldo__gt=0,
            ).exclude(estatus=CuentaPorPagar.EstatusCxP.CANCELADA)
        if folio:
            qs = qs.filter(factura_proveedor__folio__icontains=folio)
        if moneda:
            qs = qs.filter(factura_proveedor__moneda_id=moneda)
        if factura_proveedor:
            qs = qs.filter(factura_proveedor_id=factura_proveedor)
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha_emision")
        return _aplicar_ordering(qs, qp, ["-fecha_emision", "-id"])

    def get_serializer_class(self):
        # El alta usa su propia clase para que el esquema OpenAPI del POST y el del
        # PUT/PATCH difieran como difieren en ejecución: ver
        # CuentaPorPagarCreateSerializer.
        if self.action == "create":
            return CuentaPorPagarCreateSerializer
        return super().get_serializer_class()

    @staticmethod
    def _lock_invoice(factura):
        # La factura se bloquea antes de crear o mover una CxP hacia ella, igual que
        # al registrarla y al borrarla. Sin el lock, una CxP que llegaba a la factura
        # mientras ésta se borraba quedaba fuera de la comprobación de pagos
        # aplicados del borrado, y la cascada se la llevaba junto con esos pagos.
        locked = FacturaProveedor.objects.select_for_update().filter(pk=factura.pk).first()
        if locked is None:
            raise ErrorDeNegocio({"factura_proveedor": "La factura de proveedor ya no existe."})
        return locked

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        fp = serializer.validated_data.get("factura_proveedor")
        _validate_fk_empresa(user, fp, "factura_proveedor", empresa)
        prov = serializer.validated_data.get("proveedor")
        if prov is not None and not _puede_ver_todo(user):
            prov_emp_id = getattr(prov, "empresa_id", None)
            if prov_emp_id not in (None, empresa.pk):
                raise ValidationError({"proveedor": "Proveedor no pertenece a la empresa."})
        invoice = self._lock_invoice(fp)
        # Una CxP manual nace cuadrada: saldo = total y Pendiente. El serializer ya
        # no acepta saldo/estatus al crear y validó total contra la factura.
        total = serializer.validated_data.get("total", Decimal("0.00"))
        # Mismo origen que la generación automática: vence cuando vence la
        # factura, salvo que el cliente mande una fecha real. Un null explícito
        # cuenta como ausente: un campo de fecha vacío en el formulario no debe
        # dejar la CxP sin vencimiento.
        due_date = {}
        if serializer.validated_data.get("fecha_vencimiento") is None:
            due_date["fecha_vencimiento"] = invoice.fecha_vencimiento
        with CuentaPorPagarService.duplicate_invoice_as_business_error(invoice.pk):
            serializer.save(
                empresa=empresa,
                saldo=total,
                estatus=CuentaPorPagar.EstatusCxP.PENDIENTE,
                **due_date,
            )

    @transaction.atomic
    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        factura = serializer.validated_data.get("factura_proveedor")
        if factura is not None:
            # Factura antes que CxP: el mismo orden que su borrado.
            self._lock_invoice(factura)
        # El candado se evalúa con la fila bloqueada y releída. PagoService también
        # bloquea la CxP antes de descontar, así que un pago concurrente no se cuela
        # entre la comprobación y el guardado.
        locked = CuentaPorPagar.objects.select_for_update().filter(pk=serializer.instance.pk).first()
        if locked is None:
            raise NotFound("La cuenta por pagar ya no existe.")
        serializer.instance = locked
        CuentaPorPagarService.ensure_frozen_fields_unchanged(locked, serializer.validated_data)
        factura_id = factura.pk if factura is not None else locked.factura_proveedor_id
        with CuentaPorPagarService.duplicate_invoice_as_business_error(
            factura_id, excluding_account_id=locked.pk
        ):
            serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        CuentaPorPagarService.ensure_account_deletable(instance)
        instance.delete()


class CobroViewSet(FinanzasBaseViewSet):
    queryset = Cobro.objects.all()
    serializer_class = CobroSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if _puede_ver_todo(user):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        cliente = qp.get("cliente") or qp.get("cliente_id")
        metodo_pago = (qp.get("metodo_pago") or "").strip()
        estatus = (qp.get("estatus") or "").strip()
        cuenta_bancaria = qp.get("cuenta_bancaria") or qp.get("cuenta_bancaria_id")
        referencia = (qp.get("referencia_operacion") or qp.get("referencia") or "").strip()
        if cliente:
            qs = qs.filter(cliente_id=cliente)
        if metodo_pago:
            qs = qs.filter(metodo_pago=metodo_pago)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if cuenta_bancaria:
            qs = qs.filter(cuenta_bancaria_id=cuenta_bancaria)
        if referencia:
            qs = qs.filter(
                Q(referencia_operacion__icontains=referencia) | Q(referencia__icontains=referencia)
            )
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha_cobro")
        return _aplicar_ordering(qs, qp, ["-fecha_cobro", "-id"])

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        cliente = serializer.validated_data.get("cliente")
        _validate_fk_empresa(user, cliente, "cliente", empresa)
        cta = serializer.validated_data.get("cuenta_bancaria")
        _validate_fk_empresa(user, cta, "cuenta_bancaria", empresa)
        detalles_data = serializer.validated_data.pop("cobro_detalles", [])
        cobro = serializer.save(empresa=empresa)
        for d in detalles_data:
            # El campo real de ``CobroDetalle`` es ``cxc``: leer
            # ``cuenta_por_cobrar`` devolvía siempre ``None`` y esta validación no
            # se ejecutaba nunca. Se compara contra la empresa del DOCUMENTO padre
            # y sin excluir al superusuario, porque una línea de otra empresa es
            # incoherente la escriba quien la escriba. ``CobroService`` sólo la
            # cazaba con ``estatus="Aplicado"``; en ``Borrador`` no corre.
            cxc = d.get("cxc")
            if cxc is not None:
                # ``CuentaPorCobrar.empresa`` es NULL-able: las filas antiguas se
                # resuelven por su factura, igual que en ``get_queryset``.
                cxc_emp = getattr(cxc, "empresa_id", None) or getattr(getattr(cxc, "factura", None), "empresa_id", None)
                if cxc_emp and cxc_emp != cobro.empresa_id:
                    raise ValidationError(
                        {"cobro_detalles": [f"La CxC {cxc.pk} no pertenece a la misma empresa que el cobro."]}
                    )
            CobroDetalle.objects.create(cobro=cobro, **d)
        if cobro.estatus == Cobro.Estatus.APLICADO:
            CobroService.aplicar_cobro(cobro)

    @transaction.atomic
    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        if serializer.instance.estatus == Cobro.Estatus.CANCELADO:
            raise ValidationError("No se puede editar un cobro cancelado.")
        anterior_estatus = serializer.instance.estatus
        cobro = serializer.save()
        nuevo_estatus = cobro.estatus
        if nuevo_estatus == Cobro.Estatus.CANCELADO and anterior_estatus != Cobro.Estatus.CANCELADO:
            CobroService.cancelar_cobro(cobro)
        elif nuevo_estatus == Cobro.Estatus.APLICADO and anterior_estatus != Cobro.Estatus.APLICADO:
            CobroService.aplicar_cobro(cobro)

    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        with transaction.atomic():
            if instance.estatus == Cobro.Estatus.APLICADO:
                CobroService.cancelar_cobro(instance)
            instance.delete()

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        cobro = self.get_object()
        with transaction.atomic():
            CobroService.cancelar_cobro(cobro)
        return Response(CobroSerializer(cobro).data)


class PagoViewSet(FinanzasBaseViewSet):
    queryset = Pago.objects.all()
    serializer_class = PagoSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if _puede_ver_todo(user):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        proveedor = qp.get("proveedor") or qp.get("proveedor_id")
        metodo_pago = (qp.get("metodo_pago") or "").strip()
        estatus = (qp.get("estatus") or "").strip()
        cuenta_bancaria = qp.get("cuenta_bancaria") or qp.get("cuenta_bancaria_id")
        referencia = (qp.get("referencia_operacion") or qp.get("referencia") or "").strip()
        if proveedor:
            qs = qs.filter(proveedor_id=proveedor)
        if metodo_pago:
            qs = qs.filter(metodo_pago=metodo_pago)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if cuenta_bancaria:
            qs = qs.filter(cuenta_bancaria_id=cuenta_bancaria)
        if referencia:
            qs = qs.filter(
                Q(referencia_operacion__icontains=referencia) | Q(referencia__icontains=referencia)
            )
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha_pago")
        return _aplicar_ordering(qs, qp, ["-fecha_pago", "-id"])

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        empresa = _resolve_empresa(user, serializer.validated_data, required=True)
        proveedor = serializer.validated_data.get("proveedor")
        _validate_fk_empresa(user, proveedor, "proveedor", empresa)
        cta = serializer.validated_data.get("cuenta_bancaria")
        _validate_fk_empresa(user, cta, "cuenta_bancaria", empresa)
        detalles_data = serializer.validated_data.pop("pago_detalles", [])
        pago = serializer.save(empresa=empresa)
        for d in detalles_data:
            # El campo real de ``PagoDetalle`` es ``cxp``: leer
            # ``cuenta_por_pagar`` devolvía siempre ``None``. Se compara contra la
            # empresa del documento padre y sin excluir al superusuario, igual que
            # en ``CobroViewSet``. ``PagoService`` repite el guard, pero sólo con
            # ``estatus="Aplicado"``; en ``Borrador`` nadie lo cubría.
            cxp = d.get("cxp")
            if cxp is not None:
                cxp_emp = getattr(cxp, "empresa_id", None)
                if cxp_emp and cxp_emp != pago.empresa_id:
                    raise ValidationError(
                        {"pago_detalles": [f"La CxP {cxp.pk} no pertenece a la misma empresa que el pago."]}
                    )
            PagoDetalle.objects.create(pago=pago, **d)
        if pago.estatus == Pago.Estatus.APLICADO:
            PagoService.aplicar_pago(pago)

    @transaction.atomic
    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(serializer.instance, "empresa_id", None) and serializer.instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        if serializer.instance.estatus == Pago.Estatus.CANCELADO:
            raise ValidationError("No se puede editar un pago cancelado.")
        anterior_estatus = serializer.instance.estatus
        pago = serializer.save()
        nuevo_estatus = pago.estatus
        if nuevo_estatus == Pago.Estatus.CANCELADO and anterior_estatus != Pago.Estatus.CANCELADO:
            PagoService.cancelar_pago(pago)
        elif nuevo_estatus == Pago.Estatus.APLICADO and anterior_estatus != Pago.Estatus.APLICADO:
            PagoService.aplicar_pago(pago)

    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa and getattr(instance, "empresa_id", None) and instance.empresa_id != empresa.pk:
                raise PermissionDenied()
        with transaction.atomic():
            if instance.estatus == Pago.Estatus.APLICADO:
                PagoService.cancelar_pago(instance)
            instance.delete()

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        pago = self.get_object()
        with transaction.atomic():
            PagoService.cancelar_pago(pago)
        return Response(PagoSerializer(pago).data)


class MovimientoBancarioViewSet(FinanzasBaseViewSet):
    queryset = MovimientoBancario.objects.all()
    serializer_class = MovimientoBancarioSerializer

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        if _puede_ver_todo(user):
            qs = super().get_queryset()
        else:
            if empresa is None:
                return MovimientoBancario.objects.none()
            qs = MovimientoBancario.objects.filter(cuenta_bancaria__empresa=empresa)
        qp = self.request.query_params
        cuenta_bancaria = qp.get("cuenta_bancaria") or qp.get("cuenta_bancaria_id")
        tipo_movimiento = (qp.get("tipo_movimiento") or "").strip()
        estatus = (qp.get("estatus") or "").strip()
        origen = (qp.get("origen") or "").strip()
        concepto = (qp.get("concepto") or "").strip()
        referencia = (qp.get("referencia") or "").strip()
        cobro = qp.get("cobro") or qp.get("cobro_id")
        pago = qp.get("pago") or qp.get("pago_id")
        if cuenta_bancaria:
            qs = qs.filter(cuenta_bancaria_id=cuenta_bancaria)
        if tipo_movimiento:
            qs = qs.filter(tipo_movimiento=tipo_movimiento)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if origen:
            qs = qs.filter(origen=origen)
        if concepto:
            qs = qs.filter(concepto__icontains=concepto)
        if referencia:
            qs = qs.filter(referencia__icontains=referencia)
        if cobro:
            qs = qs.filter(cobro_id=cobro)
        if pago:
            qs = qs.filter(pago_id=pago)
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha")
        return _aplicar_ordering(qs, qp, ["-fecha", "-id"])

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        superuser = _puede_ver_todo(user)
        cta = serializer.validated_data.get("cuenta_bancaria")
        if cta is None:
            raise ValidationError({"cuenta_bancaria": "Requerida."})
        empresa = getattr(cta, "empresa", None)
        if not superuser and empresa and getattr(user, "empresa", None):
            if empresa.pk != user.empresa.pk:
                raise PermissionDenied()
        if not superuser:
            user_emp = getattr(user, "empresa", None)
            if user_emp is None:
                raise PermissionDenied("Usuario sin empresa.")
        movimiento = serializer.save()
        MovimientoBancarioService.registrar_movimiento(
            movimiento, empresa_id=getattr(empresa, "pk", None)
        )

    @transaction.atomic
    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa:
                cta = getattr(serializer.instance, "cuenta_bancaria", None)
                if cta and getattr(cta, "empresa_id", None) and cta.empresa_id != empresa.pk:
                    raise PermissionDenied()
        raise ValidationError(
            "Los movimientos bancarios generados por cobros/pagos no se deben editar directamente."
        )

    @transaction.atomic
    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa:
                cta = getattr(instance, "cuenta_bancaria", None)
                if cta and getattr(cta, "empresa_id", None) and cta.empresa_id != empresa.pk:
                    raise PermissionDenied()
        MovimientoBancarioService.revertir_movimiento(instance)
        instance.delete()

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        mb = self.get_object()
        with transaction.atomic():
            MovimientoBancarioService.revertir_movimiento(mb)
        return Response(MovimientoBancarioSerializer(mb).data)


class ConciliacionBancariaViewSet(FinanzasBaseViewSet):
    queryset = ConciliacionBancaria.objects.all()
    serializer_class = ConciliacionBancariaSerializer

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        if _puede_ver_todo(user):
            qs = super().get_queryset()
        else:
            if empresa is None:
                return ConciliacionBancaria.objects.none()
            qs = ConciliacionBancaria.objects.filter(cuenta_bancaria__empresa=empresa)
        qp = self.request.query_params
        cuenta_bancaria = qp.get("cuenta_bancaria") or qp.get("cuenta_bancaria_id")
        estatus = (qp.get("estatus") or "").strip()
        if cuenta_bancaria:
            qs = qs.filter(cuenta_bancaria_id=cuenta_bancaria)
        if estatus:
            qs = qs.filter(estatus=estatus)
        fi = qp.get("fecha_inicio") or qp.get("fecha_desde")
        ff = qp.get("fecha_fin") or qp.get("fecha_hasta")
        if fi:
            qs = qs.filter(fecha_final__gte=fi)
        if ff:
            qs = qs.filter(fecha_inicio__lte=ff)
        return _aplicar_ordering(qs, qp, ["-fecha_final", "-id"])

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        superuser = _puede_ver_todo(user)
        cta = serializer.validated_data.get("cuenta_bancaria")
        if cta is None:
            raise ValidationError({"cuenta_bancaria": "Requerida."})
        cta_emp = getattr(cta, "empresa", None)
        if not superuser:
            user_emp = getattr(user, "empresa", None)
            if user_emp is None:
                raise PermissionDenied("Usuario sin empresa.")
            if cta_emp and cta_emp.pk != user_emp.pk:
                raise ValidationError({"cuenta_bancaria": "Cuenta no pertenece a la empresa."})
        serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa:
                cta = getattr(serializer.instance, "cuenta_bancaria", None)
                if cta and getattr(cta, "empresa_id", None) and cta.empresa_id != empresa.pk:
                    raise PermissionDenied()
        if serializer.instance.estatus == ConciliacionBancaria.Estatus.CERRADA:
            raise ValidationError("No se puede editar una conciliación cerrada.")
        serializer.save()

    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa:
                cta = getattr(instance, "cuenta_bancaria", None)
                if cta and getattr(cta, "empresa_id", None) and cta.empresa_id != empresa.pk:
                    raise PermissionDenied()
        if instance.estatus == ConciliacionBancaria.Estatus.CERRADA:
            raise ValidationError("No se puede eliminar una conciliación cerrada. Cancelela primero.")
        instance.delete()

    @action(detail=False, methods=["post"], url_path="preparar")
    def preparar(self, request):
        s = ConciliacionPrepararInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = request.user
        superuser = _puede_ver_todo(user)
        data = s.validated_data
        if superuser:
            cta_id = data.get("cuenta_bancaria")
            if cta_id:
                cta = CuentaBancaria.objects.filter(pk=cta_id).select_related("empresa").first()
                if cta is None:
                    raise ValidationError({"cuenta_bancaria": "Cuenta no encontrada."})
                empresa = cta.empresa
            else:
                empresa = None
        else:
            empresa = getattr(user, "empresa", None)
        if empresa is None:
            raise PermissionDenied("Usuario sin empresa.")
        if not superuser:
            cta = CuentaBancaria.objects.filter(pk=data["cuenta_bancaria"], empresa=empresa).first()
            if cta is None:
                raise ValidationError({"cuenta_bancaria": "Cuenta no pertenece a la empresa."})
        resultado = ConciliacionService.preparar_conciliacion(
            empresa=empresa,
            cuenta_bancaria_id=data["cuenta_bancaria"],
            fecha_inicio=data.get("fecha_inicio"),
            fecha_final=data.get("fecha_final"),
            saldo_estado_cuenta=data.get("saldo_estado_cuenta") or Decimal("0.00"),
        )
        return Response(resultado, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="cerrar")
    def cerrar(self, request, pk=None):
        conciliacion = self.get_object()
        user = request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa:
                cta = getattr(conciliacion, "cuenta_bancaria", None)
                if cta and getattr(cta, "empresa_id", None) and cta.empresa_id != empresa.pk:
                    raise PermissionDenied()
        with transaction.atomic():
            ConciliacionService.cerrar_conciliacion(conciliacion)
        return Response(ConciliacionBancariaSerializer(conciliacion).data)

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        conciliacion = self.get_object()
        if conciliacion.estatus == ConciliacionBancaria.Estatus.CANCELADA:
            return Response(ConciliacionBancariaSerializer(conciliacion).data)
        conciliacion.estatus = ConciliacionBancaria.Estatus.CANCELADA
        conciliacion.save()
        return Response(ConciliacionBancariaSerializer(conciliacion).data)


# Ciclo de vida del estatus de una nota de crédito, como pares
# (actual, solicitado) que ``perform_update`` acepta. Sólo hay uno: emitir un
# borrador. El resto de los movimientos no son ediciones de un campo:
#
#   - crear en Borrador o en Emitida  -> ``perform_create``
#   - Emitida -> Cancelada            -> acción ``cancelar``, que es la única
#     que devuelve el importe acreditado a la cuenta por cobrar
#
# ``estatus`` es escribible (el serializer declara ``fields = "__all__"``) y
# ``perform_update`` sólo bloqueaba editar una nota ya Cancelada, así que dos
# rutas secuenciales corrompían el saldo: bajar una Emitida a Borrador no
# revertía la CxC y la siguiente emisión volvía a descontar el total, y un
# ``PATCH estatus="Cancelada"`` cancelaba sin devolver nada y dejaba la nota
# inservible (ya no se puede editar por cancelada, y la acción ``cancelar`` sale
# por su guard). Un cambio de estatus fuera de esta tabla es ahora un 400.
TRANSICIONES_ESTATUS_NOTA_CREDITO = frozenset(
    {
        (NotaCredito.Estatus.BORRADOR, NotaCredito.Estatus.EMITIDA),
    }
)


def _mensaje_transicion_invalida(anterior, solicitado):
    """Motivo accionable del rechazo, en términos del saldo de la CxC."""
    if solicitado == NotaCredito.Estatus.CANCELADA:
        return (
            "Una nota de crédito no se cancela editando su estatus: usa la "
            "acción cancelar (POST /notas-credito/{id}/cancelar/), que además "
            "devuelve el importe acreditado al saldo de la cuenta por cobrar."
        )
    if anterior == NotaCredito.Estatus.EMITIDA:
        return (
            "Una nota de crédito emitida no puede regresar a Borrador: su "
            "importe ya se aplicó a la cuenta por cobrar. Cancélala para "
            "devolver el saldo."
        )
    return (
        f"Transición de estatus no permitida: de {anterior} a {solicitado}."
    )


class NotaCreditoViewSet(FinanzasBaseViewSet):
    queryset = NotaCredito.objects.all()
    serializer_class = NotaCreditoSerializer

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, "empresa", None)
        if _puede_ver_todo(user):
            qs = super().get_queryset()
        else:
            if empresa is None:
                return NotaCredito.objects.none()
            qs = NotaCredito.objects.filter(factura__empresa=empresa)
        qp = self.request.query_params
        cliente = qp.get("cliente") or qp.get("cliente_id")
        estatus = (qp.get("estatus") or "").strip()
        folio = (qp.get("folio") or "").strip()
        factura = qp.get("factura") or qp.get("factura_id")
        motivo = (qp.get("motivo") or "").strip()
        if cliente:
            qs = qs.filter(cliente_id=cliente)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if folio:
            qs = qs.filter(folio__icontains=folio)
        if factura:
            qs = qs.filter(factura_id=factura)
        if motivo:
            qs = qs.filter(motivo__icontains=motivo)
        qs = _aplicar_filtros_fecha(qs, qp, fecha_campo="fecha_emision")
        return _aplicar_ordering(qs, qp, ["-fecha_emision", "-id"])

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        superuser = _puede_ver_todo(user)
        factura = serializer.validated_data.get("factura")
        cliente = serializer.validated_data.get("cliente")
        user_emp = getattr(user, "empresa", None)
        if not superuser:
            if user_emp is None:
                raise PermissionDenied("Usuario sin empresa.")
            if factura:
                fact_emp = getattr(factura, "empresa_id", None)
                if fact_emp and fact_emp != user_emp.pk:
                    raise ValidationError({"factura": "Factura no pertenece a la empresa."})
            if cliente:
                cli_emp = getattr(cliente, "empresa_id", None)
                if cli_emp and cli_emp != user_emp.pk:
                    raise ValidationError({"cliente": "Cliente no pertenece a la empresa."})
        detalles_data = serializer.validated_data.pop("nota_credito_detalles", [])
        nota = serializer.save()
        for detalle_data in detalles_data:
            factura_detalle = detalle_data.get("factura_detalle")
            if factura_detalle is not None and factura_detalle.factura_id != nota.factura_id:
                raise ValidationError({"factura_detalle": "Detalle de factura no corresponde a la factura de la nota."})
            NotaCreditoDetalle.objects.create(nota_credito=nota, **detalle_data)
        if nota.estatus == NotaCredito.Estatus.EMITIDA:
            NotaCreditoService.aplicar_nota_credito(nota)

    @transaction.atomic
    def perform_update(self, serializer):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa:
                fact = getattr(serializer.instance, "factura", None)
                if fact and getattr(fact, "empresa_id", None) and fact.empresa_id != empresa.pk:
                    raise PermissionDenied()
        # La decisión "esta petición es la que emite" se toma con la fila de la
        # nota bloqueada. Leyendo el estatus previo del objeto cargado antes del
        # lock, dos emisiones concurrentes veían ambas "Borrador" y aplicaban el
        # crédito dos veces a la CxC. Orden de bloqueo: nota -> CxC, el mismo que
        # ya usa NotaCreditoService.
        bloqueada = (
            NotaCredito.objects.select_for_update()
            .filter(pk=serializer.instance.pk)
            .first()
        )
        if bloqueada is None:
            # La nota se borró entre get_object() y el lock. Seguir con la
            # instancia obsoleta haría que save() la reinsertara con el mismo pk
            # (UPDATE de 0 filas -> INSERT) y aplicara el crédito sobre una nota
            # que ya no existía.
            raise NotFound("La nota de crédito ya no existe.")
        # Se reemplaza la instancia obsoleta para no reescribir con save() los
        # valores leídos antes del lock.
        serializer.instance = bloqueada
        if serializer.instance.estatus == NotaCredito.Estatus.CANCELADA:
            raise ValidationError("No se puede editar una nota de crédito cancelada.")
        anterior = serializer.instance.estatus
        # El estatus solicitado se compara contra el de la fila bloqueada, no
        # contra el que traía la instancia obsoleta: un cliente con la lista
        # desactualizada no puede colar una transición leyendo un estado viejo.
        # Si el cuerpo no trae ``estatus`` (una edición de cabecera, o un PUT
        # sin el campo) no hay transición que validar.
        solicitado = serializer.validated_data.get("estatus", anterior)
        if solicitado != anterior and (
            (anterior, solicitado) not in TRANSICIONES_ESTATUS_NOTA_CREDITO
        ):
            raise ErrorDeNegocio(
                {"estatus": _mensaje_transicion_invalida(anterior, solicitado)}
            )
        nota = serializer.save()
        if nota.estatus == NotaCredito.Estatus.EMITIDA and anterior != NotaCredito.Estatus.EMITIDA:
            NotaCreditoService.aplicar_nota_credito(nota)

    @transaction.atomic
    def perform_destroy(self, instance):
        user = self.request.user
        if not _puede_ver_todo(user):
            empresa = getattr(user, "empresa", None)
            if empresa:
                fact = getattr(instance, "factura", None)
                if fact and getattr(fact, "empresa_id", None) and fact.empresa_id != empresa.pk:
                    raise PermissionDenied()
        # Mismo bloqueo y relectura que perform_update, y en el mismo orden
        # nota -> CxC. El estatus se leía del objeto cargado por get_object(),
        # fuera de toda transacción: una emisión concurrente podía aplicar el
        # crédito y confirmar mientras este borrado seguía viendo "Borrador",
        # dejando la CxC rebajada sin ningún documento que lo respalde.
        bloqueada = (
            NotaCredito.objects.select_for_update().filter(pk=instance.pk).first()
        )
        if bloqueada is None:
            raise NotFound("La nota de crédito ya no existe.")
        if bloqueada.estatus == NotaCredito.Estatus.EMITIDA:
            raise ValidationError("No se puede eliminar una nota de crédito emitida. Cancelela primero.")
        bloqueada.delete()

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        nota = self.get_object()
        try:
            with transaction.atomic():
                NotaCreditoService.cancelar_nota_credito(nota)
        except NotaCredito.DoesNotExist:
            # Borrada entre get_object() y el lock del servicio.
            raise NotFound("La nota de crédito ya no existe.")
        return Response(NotaCreditoSerializer(nota).data)


class AlertaMoraViewSet(FinanzasBaseReadOnlyViewSet):
    queryset = AlertaMora.objects.all()
    serializer_class = AlertaMoraSerializer

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if _puede_ver_todo(user):
            pass
        else:
            empresa = getattr(user, "empresa", None)
            if not empresa:
                return qs.none()
            qs = qs.filter(empresa=empresa)
        qp = self.request.query_params
        tipo_cuenta = (qp.get("tipo_cuenta") or "").strip()
        nivel = (qp.get("nivel") or "").strip()
        cxc = qp.get("cuenta_por_cobrar") or qp.get("cxc")
        cxp = qp.get("cuenta_por_pagar") or qp.get("cxp")
        notificado = qp.get("notificado")
        if tipo_cuenta:
            qs = qs.filter(tipo_cuenta=tipo_cuenta)
        if nivel:
            qs = qs.filter(nivel=nivel)
        if cxc:
            qs = qs.filter(cuenta_por_cobrar_id=cxc)
        if cxp:
            qs = qs.filter(cuenta_por_pagar_id=cxp)
        if notificado is not None:
            qs = qs.filter(notificado=_bool_param(notificado))
        return _aplicar_ordering(qs, qp, ["-dias_mora", "-fecha_generada"])

    @action(detail=False, methods=["post"], url_path="generar")
    def generar(self, request):
        user = request.user
        superuser = _puede_ver_todo(user)
        qp = request.query_params
        empresa = None
        if superuser:
            empresa_id = request.data.get("empresa") or request.data.get("empresa_id") or qp.get("empresa") or qp.get("empresa_id")
            if empresa_id:
                from nucleo.models import Empresa
                empresa = Empresa.objects.filter(pk=empresa_id).first()
                if empresa is None:
                    raise ValidationError({"empresa": "Empresa no encontrada."})
        else:
            empresa = getattr(user, "empresa", None)
            if empresa is None:
                raise PermissionDenied()
        total = AlertaMoraService.generar_alertas(empresa=empresa)
        return Response({"alertas_generadas": total})


class DashboardFinancieroViewSet(FinanzasBaseSimpleViewSet):
    http_method_names = ["get"]

    def list(self, request):
        user = request.user
        superuser = _puede_ver_todo(user)
        qp = request.query_params
        if superuser:
            empresa_id = qp.get("empresa") or qp.get("empresa_id")
            if empresa_id:
                from nucleo.models import Empresa
                empresa = Empresa.objects.filter(pk=empresa_id).first()
                if empresa is None:
                    raise ValidationError({"empresa": "Empresa no encontrada."})
            else:
                empresa = getattr(user, "empresa", None)
                if empresa is None:
                    raise ValidationError({"empresa": "Superusuario debe especificar ?empresa_id=X para filtrar el dashboard."})
        else:
            empresa = getattr(user, "empresa", None)
            if empresa is None:
                raise PermissionDenied("Usuario sin empresa.")
        fecha_inicio = qp.get("fecha_inicio") or qp.get("fecha_desde")
        fecha_fin = qp.get("fecha_fin") or qp.get("fecha_hasta")
        moneda = qp.get("moneda") or qp.get("moneda_id")
        return Response(
            DashboardFinancieroService.obtener_resumen(
                empresa=empresa,
                fecha_inicio=fecha_inicio,
                fecha_fin=fecha_fin,
                moneda_id=moneda,
            )
        )
