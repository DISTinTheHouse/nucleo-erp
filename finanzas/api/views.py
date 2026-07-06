from decimal import Decimal

from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from finanzas.models import Factura, FacturaDetalle
from finanzas.api.serializers import (
    FacturaSerializer,
    FacturaDetalleSerializer,
    FacturaDesdePedidoInputSerializer,
)
from finanzas.services.factura_service import FacturaService
from finanzas.utils.folios import generate_factura_folio
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
