from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from finanzas.models import (
    CentroCosto,
    CuentaContable,
    CuentaPorCobrar,
    Factura,
    FacturaDetalle,
    Poliza,
    PolizaDetalle,
)
from finanzas.api.serializers import (
    CuentaPorCobrarSerializer,
    FacturaSerializer,
    FacturaDetalleSerializer,
    FacturaDesdePedidoInputSerializer,
    FacturaPendienteCobroInputSerializer,
)
from finanzas.services.factura_service import FacturaService
from finanzas.utils.folios import generate_factura_folio
from nucleo.models import Moneda, Sucursal
from ventas.models import Pedido, PedidoDetalle
from terceros.models import Cliente
from terceros.api.serializers import ClienteSerializer

class ClienteViewSetContabilidad(viewsets.ModelViewSet):
    queryset = Cliente.objects.filter(activo=True)
    serializer_class = ClienteSerializer
    http_method_names = ['get']

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None: return Cliente.objects.none()
        queryset = Cliente.objects.filter(empresa=empresa)
        return queryset


# Compatibilidad temporal para imports antiguos en despliegues o rutas rezagadas.
ClienteViewSet = ClienteViewSetContabilidad


class CuentaPorCobrarViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CuentaPorCobrarSerializer
    http_method_names = ['get']

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None:
            return CuentaPorCobrar.objects.none()

        qs = (
            CuentaPorCobrar.objects.select_related(
                'cliente',
                'factura',
                'factura__moneda',
            )
            .filter(factura__empresa=empresa, factura__activo=True)
            .order_by('-fecha_emision', '-id')
        )

        qp = self.request.query_params
        cliente_id = qp.get('cliente') or qp.get('cliente_id')
        estatus = (qp.get('estatus') or '').strip()
        saldo_pendiente = (qp.get('saldo_pendiente') or '').strip().lower()
        vencidas = (qp.get('vencidas') or '').strip().lower()

        if cliente_id:
            qs = qs.filter(cliente_id=cliente_id)
        if estatus:
            qs = qs.filter(estatus=estatus)
        if saldo_pendiente in {'1', 'true', 'si', 'sí'}:
            qs = qs.filter(saldo__gt=0)
        if vencidas in {'1', 'true', 'si', 'sí'}:
            qs = qs.filter(
                fecha_vencimiento__isnull=False,
                fecha_vencimiento__lt=timezone.localdate(),
                saldo__gt=0,
            ).exclude(estatus=CuentaPorCobrar.EstatusCxC.CANCELADA)

        return qs


class FacturaViewSet(viewsets.ModelViewSet):
    serializer_class = FacturaSerializer
    http_method_names = ['delete', 'get', 'post']

    def get_queryset(self):
        user = self.request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None: return Factura.objects.none()
        queryset = Factura.objects.filter(empresa=empresa)
        return queryset

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
        return poliza

    @action(detail=False, methods=['post'], url_path='registrar-pendiente-cobro')
    def registrar_pendiente_cobro(self, request):
        serializer = FacturaPendienteCobroInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user
        empresa = getattr(user, 'empresa', None)
        if empresa is None:
            raise ValidationError({'empresa': 'El usuario no tiene empresa asignada.'})

        sucursal = self._get_default_sucursal(user, empresa)
        if sucursal is None:
            raise ValidationError({'sucursal': 'No hay una sucursal disponible para registrar la factura.'})

        cliente = Cliente.objects.filter(
            pk=data['cliente'],
            empresa=empresa,
            activo=True,
        ).first()
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
            pedido = Pedido.objects.filter(pk=pedido_id, empresa=empresa).first()
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

    @action(detail=False, methods=['get', 'post'], url_path='onboarding', url_name='onboarding')
    def onboarding(self, request):
        if request.method == 'GET':
            queryset = self.filter_queryset(self.get_queryset())
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        elif request.method == 'POST':
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            factura = FacturaService.store_factura(request.user, serializer.validated_data)
            serializer = self.get_serializer(factura)
            return Response(serializer.data)

    @action(detail=False, methods=['post'], url_path='desde-pedido', url_name='desde-pedido')
    def desde_pedido(self, request):
        """
        Factura la TOTALIDAD de un Pedido en una sola exhibición.

        Body: {"pedido": <id>}. No se acepta selección de líneas ni cantidades:
        el detalle se enumera íntegramente en el servidor desde el Pedido.

        Regla de negocio: un Pedido se factura completo y una sola vez. Este
        endpoint aplica un guard de doble facturación (aislado del flujo de
        `FacturaService.store_factura`, que permanece sin tocar).
        """
        input_serializer = FacturaDesdePedidoInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        pedido_id = input_serializer.validated_data['pedido']

        user = request.user
        empresa = getattr(user, 'empresa', None)
        sucursal = getattr(user, 'sucursal_default', None)

        if empresa is None:
            raise ValidationError({'empresa': 'El usuario no tiene empresa asignada.'})

        with transaction.atomic():
            # Bloqueo del Pedido: dos requests simultáneos para el mismo
            # pedido_id se serializan aquí; el segundo espera al commit del
            # primero y luego el guard lo rechaza.
            pedido = (
                Pedido.objects.select_for_update()
                .filter(pk=pedido_id, empresa=empresa)
                .first()
            )
            if pedido is None:
                raise NotFound('El pedido no existe o no pertenece a tu empresa.')

            # Guard de doble facturación.
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
        """
        Construye la Factura + FacturaDetalle a partir de TODOS los
        PedidoDetalle del Pedido.

        Limitación estructural: FacturaDetalle apunta a PedidoDetalle (no a
        PedidoDetalleTalla) y no tiene campo `talla`, por lo que solo puede
        representar granularidad por PedidoDetalle. Las cantidades reales viven
        por talla, así que se agregan (suma) las tallas de cada línea en un
        único FacturaDetalle. Esta agregación es explícita y reportada, no
        silenciosa.
        """
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
            # Cantidad total de la línea = suma de las cantidades por talla.
            cantidad = Decimal(sum(t.cantidad for t in det.tallas.all()))
            precio_unitario = det.precio_unitario or Decimal('0')

            # descuento/impuesto quedan en 0 (mismo criterio que el TODO
            # pendiente en store_factura; fuera de alcance en esta tarea).
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
