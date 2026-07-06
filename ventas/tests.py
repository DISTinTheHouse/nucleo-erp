from django.test import TestCase
from rest_framework.test import APIClient

from auditoria.models import AuditoriaEvento
from catalogo.models import Color, Producto, ProductoVariante, Talla, TipoProducto
from inventarios.models import Almacen, Existencia, MovimientoInventario, TipoMovimiento
from nucleo.models import Empresa, Moneda, SerieFolio, Sucursal, UnidadMedida
from terceros.models import Cliente
from usuarios.models import Usuario
from ventas.models import Cotizacion, CotizacionDetalle, CotizacionDetalleTalla, Pedido


class CotizacionAutorizarInventarioTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.moneda = Moneda.objects.create(codigo_iso="MXN", nombre="Peso mexicano")
        self.empresa = Empresa.objects.create(
            codigo="empresa-ventas-test",
            razon_social="Empresa Ventas Test",
            moneda_base=self.moneda,
        )
        self.sucursal = Sucursal.objects.create(
            empresa=self.empresa,
            codigo="MTY",
            nombre="Matriz",
        )
        self.user = Usuario.objects.create_superuser(
            email="mesa-control@example.com",
            username="mesa-control",
            password="secret123",
            empresa=self.empresa,
            sucursal_default=self.sucursal,
        )
        self.user.sucursales.add(self.sucursal)
        self.client.force_authenticate(user=self.user)

        self.unidad = UnidadMedida.objects.create(clave="PZA", nombre="Pieza")
        self.tipo_producto = TipoProducto.objects.create(codigo="PT-VENTA")
        self.color = Color.objects.create(
            nombre="Negro",
            codigo="NEG",
            codigo_hex="#000000",
        )
        self.talla = Talla.objects.create(nombre="M")
        self.producto = Producto.objects.create(
            empresa=self.empresa,
            nombre="Playera Polo",
            unidad_medida=self.unidad,
            tipo=self.tipo_producto,
        )
        self.variante = ProductoVariante.objects.create(
            producto=self.producto,
            empresa=self.empresa,
            color=self.color,
            talla=self.talla,
            sku="POLO-NEG-M",
            precio_base="100.00",
        )
        self.cliente = Cliente.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            nombre="Cliente Demo",
            razon_social="Cliente Demo SA de CV",
            correo="cliente@example.com",
            rfc="XAXX010101000",
        )
        self.serie_pedido = SerieFolio.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            tipo_documento="PEDIDO",
            serie="PD",
            folio_actual=0,
            folio_inicial=1,
            activo=True,
        )
        self.almacen = Almacen.objects.create(
            empresa=self.empresa,
            sucursal=self.sucursal,
            codigo="PT",
            nombre="Almacen PT",
            estatus="ACTIVO",
            permite_salida=True,
        )
        self.existencia = Existencia.objects.create(
            producto=self.producto,
            producto_variante=self.variante,
            almacen=self.almacen,
            ubicacion=None,
            stock=5,
            cantidad="5.0000",
        )
        self.cotizacion = Cotizacion.objects.create(
            empresa=self.empresa,
            vendedor=self.user,
            sucursal=self.sucursal,
            cliente=self.cliente,
            moneda=self.moneda,
            estatus=2,
            persona_pagos="Tesoreria",
            correo_facturas="facturas@example.com",
            telefono_pagos="8110000000",
            forma_pago=Cotizacion.FormaPago.TRANSFERENCIA,
            metodo_pago=Cotizacion.MetodoPago.PUE,
            uso_cfdi=Cotizacion.UsoCfdi.GO3,
            subtotal="300.00",
            gran_total="348.00",
        )
        self.cotizacion_detalle = CotizacionDetalle.objects.create(
            cotizacion=self.cotizacion,
            producto=self.producto,
            color=self.color,
            precio_lista="100.00",
            precio_unitario="100.00",
            subtotal_linea="300.00",
        )
        CotizacionDetalleTalla.objects.create(
            cotizacion_detalle=self.cotizacion_detalle,
            talla=self.talla,
            cantidad=3,
            precio_unitario="100.00",
            subtotal_talla="300.00",
            sku=self.variante.sku,
            variante=self.variante,
        )

    def _autorizar_cotizacion(self):
        response = self.client.post(
            f"/api/v1/ventas/cotizaciones/{self.cotizacion.pk}/autorizar/",
            {},
            format="json",
            secure=True,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.cotizacion.refresh_from_db()
        return Pedido.objects.get(cotizacion=self.cotizacion)

    def test_autorizar_descuenta_existencias_y_registra_movimiento(self):
        pedido = self._autorizar_cotizacion()
        self.existencia.refresh_from_db()
        movimiento = MovimientoInventario.objects.get(pedido=pedido)
        auditoria = AuditoriaEvento.objects.get(
            modulo="inventarios",
            accion="SALIDA",
            id_registro=str(pedido.pk),
        )

        self.assertEqual(self.cotizacion.estatus, 3)
        self.assertIsNotNone(pedido.folio)
        self.assertEqual(pedido.serie_folio_id, self.serie_pedido.pk)
        self.assertEqual(str(self.existencia.cantidad), "2.0000")
        self.assertEqual(self.existencia.stock, 2)

        self.assertEqual(movimiento.tipo_movimiento, TipoMovimiento.SALIDA)
        self.assertEqual(movimiento.pedido_id, pedido.pk)
        self.assertIn(pedido.folio, movimiento.observaciones)

        self.assertEqual(auditoria.empresa_id, self.empresa.pk)
        self.assertEqual(auditoria.despues_json["pedido_id"], pedido.pk)
        self.assertEqual(auditoria.despues_json["items"][0]["producto_variante_id"], self.variante.pk)

    def test_aceptar_cambios_incrementa_salida_por_delta(self):
        pedido = self._autorizar_cotizacion()

        talla = CotizacionDetalleTalla.objects.get(cotizacion_detalle=self.cotizacion_detalle)
        talla.cantidad = 4
        talla.subtotal_talla = "400.00"
        talla.save(update_fields=["cantidad", "subtotal_talla"])
        self.cotizacion.estatus = 5
        self.cotizacion.save(update_fields=["estatus", "updated_at"])

        response = self.client.post(
            f"/api/v1/ventas/cotizaciones/{self.cotizacion.pk}/aceptar-cambios/",
            {},
            format="json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.existencia.refresh_from_db()
        self.cotizacion.refresh_from_db()

        self.assertEqual(self.cotizacion.estatus, 3)
        self.assertEqual(str(self.existencia.cantidad), "1.0000")

        movimientos = MovimientoInventario.objects.filter(pedido=pedido).order_by("id")
        self.assertEqual(movimientos.count(), 2)
        self.assertEqual(movimientos.last().tipo_movimiento, TipoMovimiento.SALIDA)
        self.assertIn("cambios aceptados", movimientos.last().observaciones)

        auditorias = AuditoriaEvento.objects.filter(
            modulo="inventarios",
            accion="SALIDA",
            id_registro=str(pedido.pk),
        ).order_by("id_evento")
        self.assertEqual(auditorias.count(), 2)
        self.assertEqual(auditorias.last().despues_json["items"][0]["delta"], "-1.0000")

    def test_aceptar_cambios_regresa_existencias_por_delta(self):
        pedido = self._autorizar_cotizacion()

        talla = CotizacionDetalleTalla.objects.get(cotizacion_detalle=self.cotizacion_detalle)
        talla.cantidad = 1
        talla.subtotal_talla = "100.00"
        talla.save(update_fields=["cantidad", "subtotal_talla"])
        self.cotizacion.estatus = 5
        self.cotizacion.save(update_fields=["estatus", "updated_at"])

        response = self.client.post(
            f"/api/v1/ventas/cotizaciones/{self.cotizacion.pk}/aceptar-cambios/",
            {},
            format="json",
            secure=True,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.existencia.refresh_from_db()
        self.cotizacion.refresh_from_db()

        self.assertEqual(self.cotizacion.estatus, 3)
        self.assertEqual(str(self.existencia.cantidad), "4.0000")

        movimientos = MovimientoInventario.objects.filter(pedido=pedido).order_by("id")
        self.assertEqual(movimientos.count(), 2)
        self.assertEqual(movimientos.last().tipo_movimiento, TipoMovimiento.ENTRADA)
        self.assertIn("Reintegro automático", movimientos.last().observaciones)

        auditoria = AuditoriaEvento.objects.filter(
            modulo="inventarios",
            accion="ENTRADA",
            id_registro=str(pedido.pk),
        ).latest("id_evento")
        self.assertEqual(auditoria.despues_json["items"][0]["delta"], "2.0000")
