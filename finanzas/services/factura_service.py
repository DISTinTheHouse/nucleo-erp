from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Sum

from finanzas.exceptions import ErrorDeNegocio
from finanzas.models import Factura, FacturaDetalle
from finanzas.utils.folios import TIPOS_DOCUMENTO_FACTURA, generate_factura_folio
from nucleo.models import SerieFolio
from ventas.models import PedidoDetalleTalla

CENTAVO = Decimal('0.01')


def _redondear(valor):
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def _texto(valor):
    return str(valor) if valor is not None else None


def _codigo_y_descripcion(catalogo_sat):
    if catalogo_sat is None:
        return None
    return {'codigo': catalogo_sat.codigo, 'descripcion': catalogo_sat.descripcion}


def _precio_de_talla(pedido_detalle_talla):
    """Precio unitario (sin IVA) de una talla: el propio de la talla si lo
    tiene, si no el del renglón del pedido."""
    if pedido_detalle_talla.precio_unitario is not None:
        return pedido_detalle_talla.precio_unitario
    return pedido_detalle_talla.pedido_detalle.precio_unitario or Decimal('0')


class FacturaService:
    """Facturación de pedidos por piezas.

    Un pedido se factura en parcialidades: cada factura toma piezas de tallas
    concretas (``PedidoDetalleTalla``) y nunca más de las pendientes. Quien
    llama debe tener el ``Pedido`` bloqueado (``select_for_update``) para que
    dos facturas simultáneas no tomen las mismas piezas.
    """

    @staticmethod
    def piezas_por_facturar(pedido):
        """Por talla del pedido: piezas pedidas, ya facturadas y pendientes.

        Cuenta como facturado todo renglón de una factura activa no cancelada;
        un Borrador también aparta sus piezas. Los renglones anteriores a la
        facturación por talla (sin ``pedido_detalle_talla``) se descuentan del
        renglón del pedido, repartidos en orden entre sus tallas.
        """
        tallas = list(
            PedidoDetalleTalla.objects.filter(pedido_detalle__pedido=pedido)
            .select_related('pedido_detalle__producto', 'talla')
            .order_by('pedido_detalle_id', 'id')
        )

        facturado_por_talla = defaultdict(Decimal)
        sin_talla_por_detalle = defaultdict(Decimal)
        renglones_facturados = (
            FacturaDetalle.objects.filter(
                pedido_detalle__pedido=pedido, factura__activo=True
            )
            .exclude(factura__estatus=Factura.FacturaStatus.CANCELADA)
            .values('pedido_detalle_id', 'pedido_detalle_talla_id')
            .annotate(cantidad_total=Sum('cantidad'))
        )
        for renglon in renglones_facturados:
            if renglon['pedido_detalle_talla_id'] is None:
                sin_talla_por_detalle[renglon['pedido_detalle_id']] += renglon['cantidad_total']
            else:
                facturado_por_talla[renglon['pedido_detalle_talla_id']] += renglon['cantidad_total']

        piezas = []
        for talla in tallas:
            pedida = Decimal(talla.cantidad)
            facturada = facturado_por_talla[talla.pk]
            pendiente = max(pedida - facturada, Decimal('0'))

            sin_talla = sin_talla_por_detalle[talla.pedido_detalle_id]
            tomar = min(pendiente, sin_talla)
            sin_talla_por_detalle[talla.pedido_detalle_id] -= tomar
            facturada += tomar
            pendiente -= tomar

            piezas.append({
                'pedido_detalle_talla': talla,
                'precio_unitario': _precio_de_talla(talla),
                'cantidad_pedida': pedida,
                'cantidad_facturada': facturada,
                'cantidad_pendiente': pendiente,
            })
        return piezas

    @staticmethod
    def desglose(factura):
        """Factura completa para consulta: emisor, receptor, pedido, conceptos
        por producto con las piezas de cada talla, importes, avance de
        facturación del pedido, parcialidades, cobranza y notas de crédito.

        Los importes van como texto, igual que en los serializers de la API.
        """
        renglones = (
            factura.factura_detalles.select_related(
                'producto__sat_prodserv',
                'producto__sat_unidad',
                'producto__unidad_medida',
                'pedido_detalle__color',
                'pedido_detalle_talla__talla',
            )
            .order_by('pedido_detalle_id', 'pedido_detalle_talla_id', 'id')
        )

        # Un pedido de otra empresa (datos previos al candado de escritura) no se lee.
        pedido = factura.pedido
        if pedido is not None and pedido.empresa_id != factura.empresa_id:
            pedido = None
        piezas_pedido = {}
        if pedido is not None:
            piezas_pedido = {
                pieza['pedido_detalle_talla'].pk: pieza
                for pieza in FacturaService.piezas_por_facturar(pedido)
            }

        conceptos = {}
        total_piezas = Decimal('0')
        for renglon in renglones:
            concepto = conceptos.get(renglon.pedido_detalle_id)
            if concepto is None:
                producto = renglon.producto
                color = renglon.pedido_detalle.color
                concepto = conceptos[renglon.pedido_detalle_id] = {
                    'pedido_detalle': renglon.pedido_detalle_id,
                    'producto': {
                        'id': producto.pk,
                        'nombre': producto.nombre,
                        'codigo': producto.codigo,
                        'descripcion': producto.descripcion,
                        'unidad_medida': getattr(producto.unidad_medida, 'clave', None),
                        'sat_clave_prodserv': _codigo_y_descripcion(producto.sat_prodserv),
                        'sat_clave_unidad': _codigo_y_descripcion(producto.sat_unidad),
                    },
                    'color': {'id': color.pk, 'nombre': color.nombre} if color else None,
                    'cantidad': Decimal('0'),
                    'subtotal': Decimal('0'),
                    'descuento': Decimal('0'),
                    'impuesto': Decimal('0'),
                    'total': Decimal('0'),
                    'tallas': [],
                }

            talla_pedido = renglon.pedido_detalle_talla
            pieza = piezas_pedido.get(getattr(talla_pedido, 'pk', None))
            concepto['tallas'].append({
                'factura_detalle': renglon.pk,
                'pedido_detalle_talla': getattr(talla_pedido, 'pk', None),
                'talla': getattr(talla_pedido, 'talla_id', None),
                'talla_nombre': talla_pedido.talla.nombre if talla_pedido else None,
                'cantidad': int(renglon.cantidad),
                'precio_unitario': _texto(renglon.precio_unitario),
                'subtotal': _texto(renglon.subtotal),
                'descuento': _texto(renglon.descuento),
                'porcentaje_impuesto': _texto(renglon.porcentaje_impuesto),
                'impuesto': _texto(renglon.impuesto),
                'total': _texto(renglon.total),
                'cantidad_pedida': int(pieza['cantidad_pedida']) if pieza else None,
                'cantidad_pendiente_pedido': int(pieza['cantidad_pendiente']) if pieza else None,
            })
            for campo in ('cantidad', 'subtotal', 'descuento', 'impuesto', 'total'):
                concepto[campo] += getattr(renglon, campo)
            total_piezas += renglon.cantidad

        for concepto in conceptos.values():
            concepto['cantidad'] = int(concepto['cantidad'])
            for campo in ('subtotal', 'descuento', 'impuesto', 'total'):
                concepto[campo] = _texto(concepto[campo])

        cliente = factura.cliente
        # Datos fiscales del receptor: los congelados en el pedido (los que se
        # usarán al timbrar) y, si no hay pedido o vienen vacíos, los del cliente.
        regimen = (getattr(pedido, 'cliente_regimen_fiscal', None)
                   or cliente.sat_regimen_fiscal)
        receptor = {
            'cliente': cliente.pk,
            'nombre': cliente.nombre,
            'razon_social': getattr(pedido, 'cliente_razon_social', None) or cliente.razon_social,
            'rfc': getattr(pedido, 'cliente_rfc', None) or cliente.rfc,
            'regimen_fiscal': _codigo_y_descripcion(regimen),
            'codigo_postal': getattr(pedido, 'cliente_codigo_postal', None) or cliente.codigo_postal,
            'correo_facturas': (getattr(pedido, 'correo_facturas', None) or cliente.correo or None),
        }

        datos_pedido = None
        avance_pedido = None
        parcialidades = []
        if pedido is not None:
            datos_pedido = {
                'id': pedido.pk,
                'folio': pedido.folio,
                'oc': pedido.oc,
                'forma_pago': pedido.forma_pago,
                'forma_pago_nombre': pedido.get_forma_pago_display(),
                'metodo_pago': pedido.metodo_pago,
                'metodo_pago_nombre': pedido.get_metodo_pago_display(),
                'uso_cfdi': pedido.uso_cfdi,
                'uso_cfdi_nombre': pedido.get_uso_cfdi_display(),
            }
            avance_pedido = {
                'piezas_pedidas': int(sum(p['cantidad_pedida'] for p in piezas_pedido.values())),
                'piezas_facturadas': int(sum(p['cantidad_facturada'] for p in piezas_pedido.values())),
                'piezas_pendientes': int(sum(p['cantidad_pendiente'] for p in piezas_pedido.values())),
            }
            parcialidades = [
                {
                    'id': otra.pk,
                    'folio': otra.folio,
                    'estatus': otra.estatus,
                    'fecha_emision': otra.fecha_emision,
                    'total': _texto(otra.total),
                    'es_esta_factura': otra.pk == factura.pk,
                }
                for otra in Factura.objects.filter(pedido=pedido, activo=True)
                .order_by('fecha_emision', 'id')
            ]

        return {
            'id': factura.pk,
            'folio': factura.folio,
            'estatus': factura.estatus,
            'activo': factura.activo,
            'fecha_emision': factura.fecha_emision,
            'fecha_vencimiento': factura.fecha_vencimiento,
            'observaciones': factura.observaciones,
            'created_at': factura.created_at,
            'emisor': {
                'empresa': factura.empresa_id,
                'razon_social': factura.empresa.razon_social,
                'nombre_comercial': factura.empresa.nombre_comercial,
                'rfc': factura.empresa.rfc,
                'sucursal': factura.sucursal_id,
                'sucursal_nombre': factura.sucursal.nombre,
            },
            'receptor': receptor,
            'pedido': datos_pedido,
            'moneda': {
                'id': factura.moneda_id,
                'codigo_iso': factura.moneda.codigo_iso,
                'nombre': factura.moneda.nombre,
                'simbolo': factura.moneda.simbolo,
            },
            'conceptos': list(conceptos.values()),
            'importes': {
                'total_piezas': int(total_piezas),
                'subtotal': _texto(factura.subtotal),
                'descuento': _texto(factura.descuento),
                'impuestos': _texto(factura.impuestos),
                'total': _texto(factura.total),
            },
            'avance_pedido': avance_pedido,
            'parcialidades': parcialidades,
            'cobranza': [
                {
                    'id': cxc.pk,
                    'estatus': cxc.estatus,
                    'total': _texto(cxc.total),
                    'saldo': _texto(cxc.saldo),
                    'fecha_vencimiento': cxc.fecha_vencimiento,
                    'fecha_ultimo_pago': cxc.fecha_ultimo_pago,
                }
                for cxc in factura.cuentas_por_cobrar.order_by('id')
            ],
            'notas_credito': [
                {
                    'id': nota.pk,
                    'folio': nota.folio,
                    'estatus': nota.estatus,
                    'motivo': nota.motivo,
                    'fecha_emision': nota.fecha_emision,
                    'total': _texto(nota.total),
                }
                for nota in factura.nota_creditos.order_by('id')
            ],
        }

    @staticmethod
    @transaction.atomic
    def store_factura(user, validated_data, sucursal):
        """Factura las piezas elegidas: ``factura_detalles`` trae
        ``pedido_detalle_talla`` + ``cantidad`` por renglón."""
        # La sucursal la resuelve y valida quien llama (``_get_default_sucursal``):
        # ``user.sucursal_default`` puede ser None o de otra empresa.
        pedido = validated_data.pop('pedido')
        if not pedido:
            raise ErrorDeNegocio({'pedido': 'El pedido no existe'})

        renglones = validated_data.pop('factura_detalles', [])
        lineas = [
            (renglon['pedido_detalle_talla'], renglon['cantidad'])
            for renglon in renglones
        ]
        return FacturaService._crear_factura(
            getattr(user, 'empresa', None), sucursal, pedido, lineas, **validated_data
        )

    @staticmethod
    @transaction.atomic
    def facturar_pendiente(pedido, empresa, sucursal):
        """Factura todas las piezas que le quedan pendientes al pedido."""
        FacturaService._exigir_pedido_vigente(pedido)
        lineas = [
            (pieza['pedido_detalle_talla'], pieza['cantidad_pendiente'])
            for pieza in FacturaService.piezas_por_facturar(pedido)
            if pieza['cantidad_pendiente'] > 0
        ]
        if not lineas:
            raise ErrorDeNegocio({'pedido': 'El pedido no tiene piezas pendientes por facturar.'})
        return FacturaService._crear_factura(empresa, sucursal, pedido, lineas)

    @staticmethod
    def _exigir_pedido_vigente(pedido):
        if pedido.estatus == pedido.ESTATUS_CANCELADO:
            raise ErrorDeNegocio({'pedido': 'El pedido está CANCELADO; no se puede facturar.'})

    @staticmethod
    def _validar_lineas(pedido, lineas):
        FacturaService._exigir_pedido_vigente(pedido)
        if not lineas:
            raise ErrorDeNegocio({
                'factura_detalles': 'La factura debe incluir al menos una línea.'
            })

        # Una factura registrada solo por monto (``registrar-pendiente-cobro``)
        # no dice qué piezas cubre: facturar piezas encima la duplicaría.
        hay_factura_por_monto = (
            Factura.objects.filter(pedido=pedido, activo=True, factura_detalles__isnull=True)
            .exclude(estatus=Factura.FacturaStatus.CANCELADA)
            .exists()
        )
        if hay_factura_por_monto:
            raise ErrorDeNegocio({
                'pedido': 'El pedido ya tiene una factura registrada por monto, sin piezas; '
                          'no admite facturación por piezas.'
            })

        pendientes = {
            pieza['pedido_detalle_talla'].pk: pieza
            for pieza in FacturaService.piezas_por_facturar(pedido)
        }
        errores = []
        vistas = set()
        for talla, cantidad in lineas:
            pieza = pendientes.get(talla.pk)
            if pieza is None:
                errores.append(f'La talla {talla.pk} no pertenece al pedido.')
                continue
            if talla.pk in vistas:
                errores.append(f'La talla {talla.pk} viene repetida.')
                continue
            vistas.add(talla.pk)

            if cantidad <= 0 or cantidad != cantidad.to_integral_value():
                errores.append(f'La talla {talla.pk}: la cantidad debe ser un número entero de piezas mayor a 0.')
                continue
            talla_pedida = pieza['pedido_detalle_talla']
            producto = talla_pedida.pedido_detalle.producto
            if producto is None:
                errores.append(
                    f'La talla {talla.pk}: el renglón del pedido no tiene producto de catálogo y no puede facturarse.'
                )
                continue
            if cantidad > pieza['cantidad_pendiente']:
                errores.append(
                    f'{producto.nombre} talla {talla_pedida.talla.nombre}: '
                    f'pendientes {pieza["cantidad_pendiente"]:.0f}, solicitadas {cantidad:.0f}.'
                )
        if errores:
            raise ErrorDeNegocio({'factura_detalles': errores})
        return pendientes

    @staticmethod
    def _crear_factura(empresa, sucursal, pedido, lineas, **campos):
        # Se valida todo antes de generar el folio para no consumirlo en balde.
        pendientes = FacturaService._validar_lineas(pedido, lineas)
        try:
            folio = generate_factura_folio(empresa, sucursal)
        except DjangoValidationError as exc:
            raise ErrorDeNegocio({'serie_folio': exc.messages})
        # La serie guardada es la de la que salió el folio (#341).
        serie_folio = SerieFolio.resolve(empresa, sucursal, TIPOS_DOCUMENTO_FACTURA)

        factura = Factura.objects.create(
            empresa=empresa,
            sucursal=sucursal,
            cliente=pedido.cliente,
            moneda=pedido.moneda,
            pedido=pedido,
            serie_folio=serie_folio,
            folio=folio,
            **campos
        )

        porcentaje_impuesto = Decimal(pedido.iva or 0)
        bulk_data = []
        factura_subtotal = Decimal('0.00')
        factura_descuento = Decimal('0.00')
        factura_impuestos = Decimal('0.00')
        factura_total = Decimal('0.00')
        for talla, cantidad in lineas:
            talla = pendientes[talla.pk]['pedido_detalle_talla']
            precio_unitario = pendientes[talla.pk]['precio_unitario']

            subtotal = _redondear(cantidad * precio_unitario)
            # Los descuentos del pedido quedan fuera de la facturación por piezas.
            descuento = Decimal('0.00')
            impuesto = _redondear((subtotal - descuento) * porcentaje_impuesto / Decimal('100'))
            total = subtotal - descuento + impuesto

            bulk_data.append(
                FacturaDetalle(
                    factura=factura,
                    pedido_detalle=talla.pedido_detalle,
                    pedido_detalle_talla=talla,
                    producto=talla.pedido_detalle.producto,
                    cantidad=cantidad,
                    precio_unitario=precio_unitario,
                    descuento=descuento,
                    porcentaje_impuesto=porcentaje_impuesto,
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
        factura.save(update_fields=['subtotal', 'descuento', 'impuestos', 'total'])
        return factura
